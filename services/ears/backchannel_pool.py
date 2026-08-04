"""Pre-rendered backchannel line pool - short real words ("right", "yeah", "got it")
played instead of the real response when the user's utterance sounds abandoned
rather than finished (services/ears/completeness.py). Non-lexical sounds ("mm",
"hmm") were tried and dropped - see _GENERATION_PROMPT and CLAUDE.md's listening
verdict: XTTS has no real pronunciation for them, so they came out as long, strange
vocalizations (1.9-2.2s for two letters) instead of a quick, clean word. Pre-rendering is
non-negotiable: generating live at the moment a backchannel is needed would already
have missed its moment - a prompt arriving 800ms after someone trails off doesn't
read as a backchannel anymore. Same pattern PROMPTS.md's A17 camera-cover reactions
will use: a background job fills a pool ahead of when it's needed.

Lines are generated in Cortana's voice (persona.md as context) via
services.brain.client, synthesized once via the shared TTS engine
(services.voice.tts._get_engine() - the same persistent instance speak_stream() uses,
not a second one), and held in memory only. "Never repeat within a session" is
explicitly session-scoped (not cross-restart), so there's no disk persistence here -
a fresh process starts with a fresh pool and a fresh used-lines set, which already
satisfies the requirement without the complexity of a durable store.

take() is synchronous and just pops a ready entry - it never generates on demand.
ensure_filled() is async and does the actual generation; the caller is responsible
for scheduling it (once eagerly at startup, then opportunistically after each take()
drops the pool below min_size) as a background task, not awaited inline on the
critical path of playing a backchannel.

Short text (every backchannel line is 1-4 words) can trigger a known XTTS failure
mode: the autoregressive decoder occasionally misses the stop token and rambles on
for several extra seconds of unrelated audio. Measured directly: 3/60 short-text
calls across 4 candidate references hit it (one "Mm-hmm?" ran 6.59s), clustered on
two of the four references and never on the other two - reference-dependent, not
universal. Lowering temperature does NOT help (made it worse - a 27s runaway at
temperature=0.4 on the worst-affected reference) - it's not a sampling-randomness
problem in the way that would fix. _synthesize_with_retry() is the actual
mitigation: a duration sanity check against the input length, retried a few times
on failure, since the failure is stochastic (same input, same reference, doesn't
fail every time) so a fresh attempt usually avoids it.
"""

import asyncio
import json
import random
import re
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from services.brain import client as brain_client
from services.voice import tts as voice_tts

ROOT = Path(__file__).resolve().parent.parent.parent
PERSONA_PATH = ROOT / "config" / "persona.md"
CONFIG_PATH = ROOT / "config" / "cortana.toml"
POOL_LOG_PATH = ROOT / "logs" / "backchannel_pool.jsonl"

# Backchannels are always 1-4 words of plain English (see _GENERATION_PROMPT) - a
# quick, cheap gate before anything reaches the tokenizer/GPU. Printable ASCII only
# (no emoji, no smart quotes/em dashes the LLM sometimes emits, no stray control
# chars) and a generous length ceiling well above anything a real backchannel line
# should ever be. This doesn't claim to be *the* fix for the srcIndex CUDA assert
# (deliberate emoji/non-ASCII/empty probes against XTTS directly didn't reproduce it -
# see xtts_engine.py's module docstring) - it's a cheap first filter for exactly the
# kind of input the live crash was suspected to involve, on top of, not instead of,
# _validate_text_tokens()'s real structural check in xtts_engine.py.
_SAFE_TEXT_RE = re.compile(r'^[\x20-\x7E]+$')
_MAX_TEXT_CHARS = 60


def _log(record: dict) -> None:
    POOL_LOG_PATH.parent.mkdir(exist_ok=True)
    with POOL_LOG_PATH.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}) + "\n")


def _is_safe_text(text: str) -> bool:
    return bool(text) and len(text) <= _MAX_TEXT_CHARS and bool(_SAFE_TEXT_RE.match(text))

_GENERATION_PROMPT = """You are generating short backchannel words for {name}, a voice \
assistant, to say while the person she's talking to trails off mid-thought - the verbal \
equivalent of a nod. It signals "I'm still here, still listening" - it does not instruct \
them to continue and does not ask them anything.

Rules:
- Real words only, always spelled as actual dictionary words: "Right.", "Yeah.", "Oh?", \
  "Got it.", "Sure.", "Okay.", "I see."
- Never a non-lexical sound or interjection spelled out phonetically - no "Mm", "Mhm", \
  "Hm", "Hmm", "Mm-hmm", or similar. XTTS has no real pronunciation for these; they come \
  out as a strange, unnaturally long vocalization instead of a quick, clean word.
- 1-2 words, almost always - genuinely short, spoken while someone is still gathering \
  their thought.
- Never a directive ("Go on.", "Continue.", "Take your time.") and never a question about \
  what they were going to say ("You were saying?", "What were you saying?") - those \
  instruct or interrupt. A real listener just says a small word and waits.
- Natural, low-key, not needy, not repetitive of each other.

Persona for voice/tone:
{persona}

Generate {n} distinct backchannel words, one per line, nothing else - no numbering, \
no quotes, no extra commentary."""


def _load_assistant_name() -> str:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config["audio"]["wake"]["verify_phrase"].capitalize()


async def _generate_lines(n: int) -> list[str]:
    persona_text = PERSONA_PATH.read_text(encoding="utf-8") if PERSONA_PATH.exists() else ""
    prompt = _GENERATION_PROMPT.format(name=_load_assistant_name(), persona=persona_text, n=n)
    chunks = []
    async for token in brain_client.stream([{"role": "user", "content": prompt}], think=False):
        chunks.append(token)
    raw = "".join(chunks)
    lines = [ln.strip().strip('"').strip("'").lstrip("-*0123456789. ") for ln in raw.splitlines()]
    return [ln for ln in lines if ln]


_MAX_DURATION_S_PER_CHAR = 0.3  # generous ceiling before a result looks like the
# short-text rambling failure rather than legitimate content - see module docstring
_MIN_MAX_DURATION_S = 2.5
_SYNTHESIZE_RETRIES = 3


async def _synthesize_with_retry(engine, text: str, **inference_overrides) -> np.ndarray:
    """Retries on an anomalously long result - see module docstring for why this is
    the real mitigation (temperature isn't). Returns whatever the last attempt
    produced if every retry still looks like a runaway, rather than blocking
    indefinitely or silently dropping the line.

    Logs the exact text before every attempt - the live A4 crash (a CUDA device-side
    assert during pool synthesis) left no record of which candidate line triggered
    it, only that a call was in progress somewhere in the pool fill. A fatal CUDA
    error (xtts_engine.py's _CudaContextPoisoned, or the underlying fatal RuntimeError
    if it reaches here first) is not retried - the context is already corrupted, so a
    second/third attempt can only add more confusing failures on top, not recover."""
    max_duration_s = max(_MIN_MAX_DURATION_S, len(text) * _MAX_DURATION_S_PER_CHAR)
    audio = np.zeros(0, dtype=np.float32)
    for attempt in range(_SYNTHESIZE_RETRIES):
        _log({"stage": "synthesize_attempt", "text": text, "attempt": attempt + 1})
        try:
            audio = await asyncio.to_thread(engine.synthesize, text, **inference_overrides)
        except Exception as exc:
            _log({"stage": "synthesize_error", "text": text, "attempt": attempt + 1, "error": repr(exc)})
            raise
        if len(audio) / engine.sample_rate <= max_duration_s:
            return audio
    return audio


def _apply_gain_db(audio: np.ndarray, db: float) -> np.ndarray:
    """Backchannels sit under the conversation, not competing with it - see
    module docstring / CLAUDE.md listening verdict. Applied once at pool-fill
    time (baked into the stored audio) rather than at playback, since every
    backchannel line only ever plays through play_audio() with no other gain
    stage in between."""
    return audio * (10 ** (db / 20))


@dataclass
class PoolEntry:
    id: str
    text: str
    audio: np.ndarray
    sample_rate: int


class BackchannelPool:
    def __init__(self, min_size: int = 5, target_size: int = 10,
                 reference: str | None = "soft", speed: float = 0.88, volume_db: float = -8.0):
        self.min_size = min_size
        self.target_size = target_size
        self.reference = reference
        self.speed = speed
        self.volume_db = volume_db
        self._pool: list[PoolEntry] = []
        self._used_texts: set[str] = set()

    def size(self) -> int:
        return len(self._pool)

    def take(self) -> PoolEntry | None:
        """Pops one ready entry at random, or None if the pool is empty - never
        blocks, never generates. An empty pool is a real possibility (startup before
        the first fill, or a burst of abandoned utterances outrunning regeneration)
        and the caller must handle it by not playing anything, not by falling back
        to live generation."""
        if not self._pool:
            return None
        idx = random.randrange(len(self._pool))
        entry = self._pool.pop(idx)
        self._used_texts.add(entry.text)
        return entry

    async def ensure_filled(self) -> int:
        """Tops the pool up to target_size if it's at or below min_size. Returns how
        many lines were added. No-op (and cheap to call speculatively) if already
        above min_size.

        Backchannels use a different reference (self.reference, "soft" by default -
        softer/more human than "calm", the real-response default) and a slower
        speed - both per-context overrides on the one shared engine instance (rule
        7), not a second engine. The reference switch is restored on the way out so
        a real response synthesized right after a pool refill still gets "calm".
        This used to be an open gap ("not locked against a concurrent real
        synthesize() call mid-refill") assumed low-probability - it wasn't:
        concurrent access here was confirmed live to corrupt shared GPT decode
        state and crash the CUDA context (see xtts_engine.py's module docstring).
        engine._model_lock now serializes every call that touches the model, so a
        concurrent real synthesize() either runs fully before or fully after this
        refill, never interleaved with it - the use_reference() calls below go
        through asyncio.to_thread specifically so waiting on that lock doesn't
        block the event loop for however long the other call takes."""
        if len(self._pool) > self.min_size:
            return 0
        needed = self.target_size - len(self._pool)
        candidates = await _generate_lines(needed + 3)  # a few extra to survive dedup filtering

        engine, _ = voice_tts._get_engine()
        prior_reference = engine.active_reference
        if self.reference and self.reference != prior_reference:
            # to_thread, not a direct call: use_reference() now holds engine._model_lock
            # for its duration (xtts_engine.py - the fix for a real concurrent-access
            # crash between this refill and a real response's synthesize() call), and
            # that lock can be held by a concurrent synthesize() call for as long as a
            # full response takes to generate. Calling it directly here would block
            # this coroutine's thread - which, for ensure_filled(), is the event loop
            # itself - for that whole duration.
            await asyncio.to_thread(engine.use_reference, self.reference)
        try:
            added = 0
            for text in candidates:
                if added >= needed:
                    break
                clean = voice_tts.sanitize(text)
                if not clean or clean in self._used_texts or any(e.text == clean for e in self._pool):
                    continue
                if not _is_safe_text(clean):
                    # LLM-generated - not guaranteed plain ASCII (emoji, smart
                    # punctuation, stray unicode have all been observed). Reject
                    # before it ever reaches the tokenizer/GPU rather than trust
                    # xtts_engine.py's _validate_text_tokens() as the only backstop.
                    _log({"stage": "reject_unsafe_text", "text": clean})
                    continue
                audio = await _synthesize_with_retry(engine, clean, speed=self.speed)
                if audio.size == 0:
                    continue
                self._pool.append(PoolEntry(
                    id=str(uuid.uuid4()), text=clean,
                    audio=_apply_gain_db(audio, self.volume_db), sample_rate=engine.sample_rate,
                ))
                added += 1
            return added
        finally:
            if prior_reference and prior_reference != engine.active_reference:
                await asyncio.to_thread(engine.use_reference, prior_reference)


_pool: BackchannelPool | None = None


def get_pool() -> BackchannelPool:
    # One persistent pool per process (rule 7) - same principle as tts.py's engine
    # singleton, just for the pre-rendered line pool instead of the model instance.
    global _pool
    if _pool is None:
        with CONFIG_PATH.open("rb") as f:
            config = tomllib.load(f)
        bc_cfg = config.get("audio", {}).get("backchannel", {})
        _pool = BackchannelPool(
            min_size=bc_cfg.get("pool_min_size", 5),
            target_size=bc_cfg.get("pool_target_size", 10),
            reference=bc_cfg.get("reference", "soft"),
            speed=bc_cfg.get("speed", 0.88),
            volume_db=bc_cfg.get("volume_db", -8.0),
        )
    return _pool
