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
        if speaker_wav is not None:
            self.set_reference(speaker_wav)
        elif default_reference is not None:
            self.use_reference(default_reference)

    def set_reference(self, speaker_wav: str | list[str]) -> None:
        """(Re)computes and caches the speaker embedding from reference audio -
        otherwise XTTS recomputes it from scratch on every call, the single biggest
        XTTS latency cost (rule 7, PROMPTS.md A3 step 3). Cached for the life of
        this engine instance, not just one call. ~30-100ms - cheap enough to call
        this on every switch, not just at startup."""
        speaker_paths = [speaker_wav] if isinstance(speaker_wav, str) else list(speaker_wav)
        self._gpt_cond_latent, self._speaker_embedding = self._model.get_conditioning_latents(
            audio_path=speaker_paths
        )
        self._active_reference = None  # path-based, not a named reference

    def use_reference(self, name: str) -> None:
        """Switch to a named reference from [voice.xtts.references] - e.g. "calm"
        for normal responses vs "soft" for softer/more human moments. Just makes the
        switch mechanically available; the logic for *when* to use which comes later
        with the persona work, not here."""
        if name not in self._references:
            raise ValueError(f"Unknown reference {name!r} - available: {list(self._references)}")
        self.set_reference(self._references[name])
        self._active_reference = name

    @property
    def active_reference(self) -> str | None:
        return self._active_reference

    def synthesize(self, text: str, **inference_overrides) -> np.ndarray:
        if self._gpt_cond_latent is None:
            raise RuntimeError(
                "XTTSEngine.synthesize() called before a reference was set - "
                "pass speaker_wav/default_reference to __init__ or call "
                "set_reference()/use_reference() first."
            )
        params = {**self._inference_defaults, **inference_overrides}
        out = self._model.inference(
            text, self._language, self._gpt_cond_latent, self._speaker_embedding, **params,
        )
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
        chunks."""
        if self._gpt_cond_latent is None:
            raise RuntimeError(
                "XTTSEngine.synthesize_stream() called before a reference was set - "
                "pass speaker_wav/default_reference to __init__ or call "
                "set_reference()/use_reference() first."
            )
        params = {**self._inference_defaults, **inference_overrides}
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stop_event = threading.Event()
        done = object()

        def _produce() -> None:
            try:
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
            await asyncio.to_thread(thread.join, 2.0)  # bounded - daemon=True is the backstop

    def close(self) -> None:
        del self._model
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
