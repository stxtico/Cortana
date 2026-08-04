"""Coqui XTTS v2 - the production TTS engine (PROMPTS.md A3), once a voice reference
is chosen from voice_refs/ candidates. Same TTSEngine interface as kokoro_engine.py:
tts.py never touches XTTS internals directly.

Two Windows-specific workarounds needed to get this running on this machine, both
library version drift, not anything wrong with the reference audio or the model:

- torchaudio.load() as of 2.9+ delegates unconditionally to TorchCodec for decoding,
  and TorchCodec's DLLs fail to load here (its own `backend=` override parameter is
  now ignored - no working non-TorchCodec path exists through torchaudio.load()
  anymore). XTTS's own load_audio() calls torchaudio.load() directly, so it's
  monkeypatched here to decode via soundfile instead - same result, sidesteps
  TorchCodec's DLL loading entirely. Same pattern as services/ears's
  compat_patch.py during wake-word training: patch the removed/changed API rather
  than fight version pins.
- coqui-tts's XTTS/tortoise code isn't compatible with transformers>=5.1 yet
  (idiap/coqui-ai-TTS#558 - ImportError on isin_mps_friendly) - transformers is
  pinned <5.1 in pyproject.toml.

Model download requires accepting Coqui's CPML (non-commercial license) interactively
unless COQUI_TOS_AGREED=1 is set - set here because the user has already explicitly
agreed to it. Don't set this without that agreement; it's a real license acceptance,
not a formality to route around.

Token-ID validation and CUDA-poisoning guard (A4 live-test crash): the live test hit
a `srcIndex < srcSelectDimSize` device-side assert in GPT-2's text-embedding lookup
during backchannel pool synthesis - a token id from the tokenizer reaching the
embedding table outside its bounds. Confirmed by inspection: Xtts.inference()'s own
path is `sent = text.strip().lower()` then `self.tokenizer.encode(sent,
lang=language)` straight into `torch.IntTensor(...)`, no bounds check anywhere
before the GPU sees it. Directly measured the tokenizer's vocab and the GPT's
text-embedding table on this model/checkpoint and found them consistent (6681 both)
- deliberate emoji/non-ASCII/empty/whitespace-only probes here didn't reproduce the
assert either (multilingual_cleaners + the BPE tokenizer's own UNK fallback seem to
absorb those), so this couldn't be pinned to one concrete input. Given that, the fix
here is structural rather than input-specific: _validate_text_tokens() below
recomputes the exact same strip/lower/encode the model itself is about to do and
checks every resulting id against the model's own text-embedding size *before*
anything reaches the GPU - whatever input eventually produces an out-of-range id,
this catches it as a clean ValueError instead of a fatal CUDA assert.
A device-side assert doesn't just fail the one call - it corrupts the whole CUDA
context for the rest of the process (confirmed live: the cleanup use_reference()
call in backchannel_pool.py's `finally` cascaded into further, unrelated-looking
CUDA errors once the assert had already fired). _CudaContextPoisoned below latches
the first fatal CUDA error this engine instance sees and makes every call after it
fail immediately with a clear message, instead of limping through more GPU calls
that only produce confusing secondary errors.

The crash recurred after the above, with _validate_text_tokens passing clean -
ruling out text tokens. The real cause: concurrent access to this engine's shared
mutable model state, not a length overflow. Diagnosed by patching
torch.nn.functional.embedding to bounds-check every lookup on the CPU side before
it reaches the GPU (catches the same failure as a clean Python exception instead of
a fatal assert - safe to iterate on). 50 sequential real synthesize() calls on
short backchannel-shaped text, including several genuine rambling-failure-mode
outputs up to 5s long, never got mel position index above 100 of the table's 608 -
ruling out "generation exceeds gpt_max_audio_tokens" as the mechanism; the model's
own max_length-bounded generate() call is doing its job. Two threads calling
synthesize() concurrently on the same engine instance, on the other hand,
reproduced an out-of-range mel_pos_embedding lookup on the first attempt (index
-100, i.e. negative - not an overflow but underflow, from GPT2InferenceModel.
forward()'s single-token decode step computing position as
`attention_mask.shape[1] - (prefix_len + 1)`, where prefix_len reads
self.cached_prefix_emb.shape[1] - shared, mutable, and clobbered mid-flight by the
other thread's concurrent store_prefix_emb()/set_reference() call). This is exactly
the "not locked against a concurrent real synthesize() call mid-refill on the
shared engine" gap backchannel_pool.py's ensure_filled() already documented as a
known, previously-unaddressed risk - it wasn't theoretical. _model_lock below (a
threading.Lock, not asyncio.Lock - synthesize() runs on asyncio.to_thread worker
threads while set_reference()/use_reference() were being called directly on the
event loop thread, so the lock has to work across both) now serializes every call
that touches shared model state.
"""

import asyncio
import os
import threading

os.environ.setdefault("COQUI_TOS_AGREED", "1")

from collections.abc import AsyncIterator

import numpy as np
import soundfile as sf
import torch
import torchaudio
import TTS.tts.models.xtts as _xtts_module
from TTS.tts.configs.xtts_config import XttsConfig
from TTS.tts.models.xtts import Xtts
from TTS.utils.manage import ModelManager

from services.voice.engine import TTSEngine

# Substrings PyTorch/CUDA use in the RuntimeErrors that follow a fatal, context-wide
# CUDA failure (device-side assert, illegal memory access, etc.) - as opposed to an
# ordinary RuntimeError that leaves the context usable. Matched case-sensitively
# against str(exc); these are the exact phrases PyTorch emits, not a guess.
_CUDA_FATAL_ERROR_MARKERS = ("device-side assert", "CUDA error", "CUDA out of memory")


class _CudaContextPoisoned(RuntimeError):
    """Raised instead of letting a call reach the GPU once this engine instance has
    already seen a fatal CUDA error. The context can't be recovered in-process -
    restart is the only real fix - so every call after the first failure should say
    that plainly rather than surface whatever unrelated-looking error it happens to
    hit next."""


def _is_fatal_cuda_error(exc: Exception) -> bool:
    return isinstance(exc, RuntimeError) and any(marker in str(exc) for marker in _CUDA_FATAL_ERROR_MARKERS)


def _validate_text_tokens(model, text: str, language: str) -> list[int]:
    """Mirrors Xtts.inference()'s own text -> token-id path exactly (strip, lower,
    then the model's own tokenizer.encode()) so this can never drift out of sync
    with what actually gets embedded. Raises ValueError - a normal, catchable
    exception - if encoding produces nothing, or produces any id outside the GPT's
    text-embedding table, instead of letting a bad id reach the GPU and trigger the
    device-side assert that corrupts the whole CUDA context."""
    sent = text.strip().lower()
    ids = model.tokenizer.encode(sent, lang=language)
    if not ids:
        raise ValueError(f"XTTS tokenizer produced no tokens for text: {text!r}")
    vocab_size = model.gpt.number_text_tokens
    bad = sorted({i for i in ids if i < 0 or i >= vocab_size})
    if bad:
        raise ValueError(
            f"XTTS tokenizer produced token id(s) outside the model's text-embedding "
            f"table (size {vocab_size}) for text {text!r}: {bad[:10]}"
        )
    return ids


def _load_audio_via_soundfile(audiopath: str, sampling_rate: int) -> torch.Tensor:
    data, sr = sf.read(audiopath, dtype="float32", always_2d=True)
    audio = torch.from_numpy(data.T)
    if audio.size(0) != 1:
        audio = torch.mean(audio, dim=0, keepdim=True)
    if sr != sampling_rate:
        audio = torchaudio.functional.resample(audio, sr, sampling_rate)
    audio.clip_(-1, 1)
    return audio


_xtts_module.load_audio = _load_audio_via_soundfile


class XTTSEngine(TTSEngine):
    sample_rate = 24000

    def __init__(self, speaker_wav: str | list[str] | None = None,
                 references: dict[str, str] | None = None, default_reference: str | None = None,
                 model_name: str = "tts_models/multilingual/multi-dataset/xtts_v2",
                 language: str = "en", device: str = "cuda",
                 temperature: float = 0.75, repetition_penalty: float = 10.0,
                 length_penalty: float = 1.0, top_k: int = 50, top_p: float = 0.85,
                 speed: float = 1.0):
        """Two ways to set up a reference: speaker_wav (a direct path, for ad-hoc/
        audition use) or references + default_reference (named, config-driven -
        [voice.xtts.references] in cortana.toml). Both end up calling set_reference()
        under the hood; speaker_wav wins if both are given.

        Inference parameters (temperature/repetition_penalty/length_penalty/top_k/
        top_p/speed) are instance defaults, overridable per-call via synthesize()'s
        kwargs - see [voice.xtts] in cortana.toml, not hardcoded here."""
        manager = ModelManager()
        model_path, config_path, _ = manager.download_model(model_name)
        config = XttsConfig()
        config.load_json(config_path)
        self._model = Xtts.init_from_config(config)
        self._model.load_checkpoint(config, checkpoint_dir=model_path, eval=True)
        self._model.to(device)
        self._language = language
        self._gpt_cond_latent = None
        self._speaker_embedding = None
        self._active_reference: str | None = None
        self._references = references or {}
        self._inference_defaults = {
            "temperature": temperature, "repetition_penalty": repetition_penalty,
            "length_penalty": length_penalty, "top_k": top_k, "top_p": top_p, "speed": speed,
        }
        self._cuda_poison_error: Exception | None = None
        # Serializes every call that touches self._model/_gpt_cond_latent/
        # _speaker_embedding or the model's own internal cached_prefix_emb/KV-cache
        # state - see module docstring for the concurrent-access corruption this
        # closes. threading.RLock, not asyncio.Lock: synthesize() runs on
        # asyncio.to_thread worker threads, so a lock usable from plain OS threads
        # is what both sides need, not one scoped to a single event loop. Reentrant
        # (not a plain Lock) because use_reference() acquires it and then calls
        # set_reference(), which acquires it again on the same thread.
        self._model_lock = threading.RLock()
        if speaker_wav is not None:
            self.set_reference(speaker_wav)
        elif default_reference is not None:
            self.use_reference(default_reference)

    def _check_not_poisoned(self) -> None:
        if self._cuda_poison_error is not None:
            raise _CudaContextPoisoned(
                "This engine's CUDA context is corrupted by a prior fatal error and "
                "cannot be used again in this process - restart it. Original error: "
                f"{self._cuda_poison_error!r}"
            ) from self._cuda_poison_error

    def set_reference(self, speaker_wav: str | list[str]) -> None:
        """(Re)computes and caches the speaker embedding from reference audio -
        otherwise XTTS recomputes it from scratch on every call, the single biggest
        XTTS latency cost (rule 7, PROMPTS.md A3 step 3). Cached for the life of
        this engine instance, not just one call. ~30-100ms - cheap enough to call
        this on every switch, not just at startup.

        Checks _check_not_poisoned() first - this is exactly the call that cascaded
        into a confusing secondary error during the live A4 crash, running as part
        of ensure_filled()'s cleanup right after the real assert had already
        corrupted the CUDA context. Holds _model_lock for the same reason
        synthesize()/synthesize_stream() do - this mutates the shared conditioning
        state a concurrent synthesize() call reads."""
        self._check_not_poisoned()
        speaker_paths = [speaker_wav] if isinstance(speaker_wav, str) else list(speaker_wav)
        with self._model_lock:
            try:
                self._gpt_cond_latent, self._speaker_embedding = self._model.get_conditioning_latents(
                    audio_path=speaker_paths
                )
            except Exception as exc:
                if _is_fatal_cuda_error(exc):
                    self._cuda_poison_error = exc
                raise
            self._active_reference = None  # path-based, not a named reference

    def use_reference(self, name: str) -> None:
        """Switch to a named reference from [voice.xtts.references] - e.g. "calm"
        for normal responses vs "soft" for softer/more human moments. Just makes the
        switch mechanically available; the logic for *when* to use which comes later
        with the persona work, not here."""
        if name not in self._references:
            raise ValueError(f"Unknown reference {name!r} - available: {list(self._references)}")
        with self._model_lock:
            self.set_reference(self._references[name])
            self._active_reference = name

    @property
    def active_reference(self) -> str | None:
        return self._active_reference

    def synthesize(self, text: str, **inference_overrides) -> np.ndarray:
        """Holds _model_lock for the whole call, not just the GPU inference -
        self._gpt_cond_latent/_speaker_embedding are read here and are exactly the
        shared state a concurrent set_reference()/use_reference() call mutates (see
        module docstring: two threads calling synthesize() concurrently on this
        engine reproduced a corrupted, negative mel position index without this)."""
        self._check_not_poisoned()
        params = {**self._inference_defaults, **inference_overrides}
        with self._model_lock:
            if self._gpt_cond_latent is None:
                raise RuntimeError(
                    "XTTSEngine.synthesize() called before a reference was set - "
                    "pass speaker_wav/default_reference to __init__ or call "
                    "set_reference()/use_reference() first."
                )
            _validate_text_tokens(self._model, text, self._language)
            try:
                out = self._model.inference(
                    text, self._language, self._gpt_cond_latent, self._speaker_embedding, **params,
                )
            except Exception as exc:
                if _is_fatal_cuda_error(exc):
                    self._cuda_poison_error = exc
                raise
        return np.asarray(out["wav"], dtype=np.float32)

    async def synthesize_stream(self, text: str, **inference_overrides) -> AsyncIterator[np.ndarray]:
        """Bridges Xtts.inference_stream() - a synchronous generator that blocks on
        each next() while the GPU generates the next chunk - into this async
        interface. Same thread+queue bridge pattern services/ears/pipeline.py uses
        for sounddevice's callback-based mic capture: a background thread drives the
        blocking generator and hands each chunk to the event loop via
        call_soon_threadsafe.

        Whole-text conditioning: the model sees all of `text` as one sequence (XTTS's
        own inference_stream doesn't re-condition per chunk), unlike splitting text
        across separate synthesize() calls - measured to matter for delivery
        character, not just gap size (CLAUDE.md's path-divergence investigation).

        Abort/barge-in: if the caller stops iterating early (breaks out of `async
        for`, calls .aclose(), or the enclosing task is cancelled), the `finally`
        block signals stop_event so the background thread stops requesting further
        chunks from XTTS instead of generating the rest of the response unseen in
        the background. Bounded by whatever chunk is already in flight (~350-420ms
        measured) - forward passes aren't preemptible mid-computation, only between
        chunks.

        _model_lock is acquired *inside* _produce(), not out here - it has to be
        held by the same thread for its whole duration (RLock.release() requires
        the owning thread), and _produce() runs on one dedicated thread for its
        entire lifetime while this outer coroutine hops between the event loop and
        asyncio.to_thread workers. The gpt_cond_latent-is-None check and
        _validate_text_tokens() moved inside the lock too, for the same reason
        synthesize() holds its lock across those reads - a concurrent
        set_reference() call is exactly the shared state that needs protecting,
        and checking it outside the lock would just move the race, not close it."""
        self._check_not_poisoned()
        params = {**self._inference_defaults, **inference_overrides}
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stop_event = threading.Event()
        done = object()

        def _produce() -> None:
            try:
                with self._model_lock:
                    if self._gpt_cond_latent is None:
                        raise RuntimeError(
                            "XTTSEngine.synthesize_stream() called before a reference was set - "
                            "pass speaker_wav/default_reference to __init__ or call "
                            "set_reference()/use_reference() first."
                        )
                    _validate_text_tokens(self._model, text, self._language)
                    stream = self._model.inference_stream(
                        text, self._language, self._gpt_cond_latent, self._speaker_embedding, **params,
                    )
                    for wav_chunk in stream:
                        if stop_event.is_set():
                            break
                        arr = wav_chunk.detach().cpu().numpy().astype(np.float32)
                        loop.call_soon_threadsafe(queue.put_nowait, arr)
                        if stop_event.is_set():
                            break
            except Exception as exc:  # surfaced on the async side, not swallowed
                if _is_fatal_cuda_error(exc):
                    # Set directly, not via call_soon_threadsafe - this is a plain
                    # attribute write (GIL-atomic), and the next call in from
                    # *any* thread must see it immediately, not after a trip
                    # through the event loop.
                    self._cuda_poison_error = exc
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, done)

        thread = threading.Thread(target=_produce, daemon=True)
        thread.start()
        try:
            while True:
                item = await queue.get()
                if item is done:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            stop_event.set()
            # No timeout - must actually finish before returning. A bounded join
            # (previously 2.0s) let a cancelled call's background thread keep
            # running CUDA ops against the shared self._model/self._gpt_cond_latent
            # after asyncio had already "moved on" - a caller starting a new
            # synthesize()/synthesize_stream() call right after cancellation would
            # then race the old thread on the same model instance. Reproduced this
            # directly: cancelling ~2s into a buffered_stream response, then
            # immediately issuing another response, segfaulted the process
            # (services/brain/loop.py's barge-in path - stop_event.is_set() is
            # only checked between GPT-token chunks, not mid-forward-pass, so the
            # in-flight chunk can legitimately take longer than any short timeout).
            # daemon=True is still the real backstop for process-exit; this join
            # protects same-process reuse, which is the actual failure mode.
            await asyncio.to_thread(thread.join)

    def close(self) -> None:
        del self._model
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
