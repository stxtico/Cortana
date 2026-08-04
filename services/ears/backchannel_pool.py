"""Pre-rendered backchannel line pool - short phrases ("and?", "go on", "you were
saying?") played instead of the real response when the user's utterance sounds
abandoned rather than finished (services/ears/completeness.py). Pre-rendering is
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
import random
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from services.brain import client as brain_client
from services.voice import tts as voice_tts

ROOT = Path(__file__).resolve().parent.parent.parent
PERSONA_PATH = ROOT / "config" / "persona.md"
CONFIG_PATH = ROOT / "config" / "cortana.toml"

_GENERATION_PROMPT = """You are generating short backchannel phrases for {name}, a voice \
assistant, to say when the person she's talking to trails off mid-thought. A backchannel \
is a verbal nudge to keep going - not a real response, not a question about what they \
were going to say.

Rules:
- Each line must be 1-4 words. Genuinely short - these get spoken while someone is \
  still gathering their thought.
- Natural, low-key, encouraging continuation - not needy, not repetitive of each other.
- Never guess or complete their thought. Never ask a real question.
- Examples of the right length and tone: "And?", "Go on.", "Mm-hmm?", "Take your time.", \
  "You were saying?", "Yeah?"

Persona for voice/tone:
{persona}

Generate {n} distinct backchannel lines, one per line, nothing else - no numbering, \
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


async def _synthesize_with_retry(engine, text: str) -> np.ndarray:
    """Retries on an anomalously long result - see module docstring for why this is
    the real mitigation (temperature isn't). Returns whatever the last attempt
    produced if every retry still looks like a runaway, rather than blocking
    indefinitely or silently dropping the line."""
    max_duration_s = max(_MIN_MAX_DURATION_S, len(text) * _MAX_DURATION_S_PER_CHAR)
    audio = np.zeros(0, dtype=np.float32)
    for _ in range(_SYNTHESIZE_RETRIES):
        audio = await asyncio.to_thread(engine.synthesize, text)
        if len(audio) / engine.sample_rate <= max_duration_s:
            return audio
    return audio


@dataclass
class PoolEntry:
    id: str
    text: str
    audio: np.ndarray
    sample_rate: int


class BackchannelPool:
    def __init__(self, min_size: int = 5, target_size: int = 10):
        self.min_size = min_size
        self.target_size = target_size
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
        above min_size."""
        if len(self._pool) > self.min_size:
            return 0
        needed = self.target_size - len(self._pool)
        candidates = await _generate_lines(needed + 3)  # a few extra to survive dedup filtering

        engine, _ = voice_tts._get_engine()
        added = 0
        for text in candidates:
            if added >= needed:
                break
            clean = voice_tts.sanitize(text)
            if not clean or clean in self._used_texts or any(e.text == clean for e in self._pool):
                continue
            audio = await _synthesize_with_retry(engine, clean)
            if audio.size == 0:
                continue
            self._pool.append(PoolEntry(
                id=str(uuid.uuid4()), text=clean, audio=audio, sample_rate=engine.sample_rate,
            ))
            added += 1
        return added


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
        )
    return _pool
