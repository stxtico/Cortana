"""Engine-agnostic streaming TTS. speak_stream() buffers LLM tokens until a sentence
boundary, then synthesizes and plays that sentence immediately while later tokens
keep arriving for the next one - never waits for the full response.

Engine picked by [voice].engine in cortana.toml. This module only ever talks to the
services.voice.engine.TTSEngine interface, never an engine's internals, so switching
Kokoro -> XTTS (PROMPTS.md A3 step 3) is a config change, not a code change.

One persistent engine instance per process (rule 7, CLAUDE.md) - model load is too
slow to redo per call, so it's lazily created once on first use and reused for the
process lifetime, with close() for shutdown.

Every sentence is sanitize()'d (strip markdown/URLs) then normalize()'d
(services/voice/normalize.py - numbers/decimals/times/units to spoken form, e.g.
"1.2mm" -> "one point two millimeters") before synthesis, applied uniformly here so
no caller has to remember to do it. Chosen over raw-digit input by ear (voice_refs/
audition/normalization_test/ and units_vs_full/) - XTTS reading raw digits was a
real part of what read as robotic.
"""

import asyncio
import json
import re
import time
import tomllib
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import sounddevice as sd

from services.voice import playback_state
from services.voice.engine import TTSEngine
from services.voice.normalize import normalize

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"
VOICE_LOG_PATH = ROOT / "logs" / "voice.jsonl"

_engine: TTSEngine | None = None
_engine_name: str | None = None

# Punctuation + optional closing quote/bracket, followed by whitespace. Trailing
# whitespace is required (not end-of-string) so a still-streaming decimal like "3.14"
# isn't split on the "3." before the rest of the token has arrived - the char right
# after "." fails the \s+ check while more digits are still incoming. Known
# limitation: "Mr. Smith" still splits early; not worth an abbreviation list yet.
_SENTENCE_END_RE = re.compile(r'[.!?]+[\'")\]]*\s+')

_CODE_FENCE_RE = re.compile(r'```.*?```', re.DOTALL)
_INLINE_CODE_RE = re.compile(r'`([^`]*)`')
_MARKDOWN_LINK_RE = re.compile(r'\[([^\]]*)\]\([^)]*\)')
_URL_RE = re.compile(r'https?://\S+')
_HEADER_RE = re.compile(r'^#{1,6}\s+', re.MULTILINE)
_LIST_MARKER_RE = re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+', re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r'^\s*>\s?', re.MULTILINE)
_EMPHASIS_RE = re.compile(r'(\*\*\*|\*\*|\*|___|__|_)(\S.*?\S|\S)\1')


def sanitize(text: str) -> str:
    """Strip markdown, code fences, list markers, and URLs so only plain prose
    reaches the synthesizer."""
    text = _CODE_FENCE_RE.sub(' ', text)
    text = _INLINE_CODE_RE.sub(r'\1', text)
    text = _MARKDOWN_LINK_RE.sub(r'\1', text)
    text = _URL_RE.sub('', text)
    text = _HEADER_RE.sub('', text)
    text = _LIST_MARKER_RE.sub('', text)
    text = _BLOCKQUOTE_RE.sub('', text)
    text = _EMPHASIS_RE.sub(r'\2', text)
    return re.sub(r'\s+', ' ', text).strip()


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)["voice"]


def _create_engine(name: str, engine_config: dict) -> TTSEngine:
    if name == "kokoro":
        from services.voice.kokoro_engine import KokoroEngine
        return KokoroEngine(**engine_config)
    if name == "xtts":
        from services.voice.xtts_engine import XTTSEngine
        return XTTSEngine(**engine_config)
    raise ValueError(f"Unknown [voice].engine: {name!r}")


def _get_engine() -> tuple[TTSEngine, str]:
    # Reused across calls - loading a TTS model per call would cost seconds, not the
    # ~280ms httpx.AsyncClient case rule 7 was written for, but the same principle.
    global _engine, _engine_name
    if _engine is None:
        config = _load_config()
        name = config["engine"]
        engine_config = config.get(name, {})
        _engine = _create_engine(name, engine_config)
        _engine_name = name
    return _engine, _engine_name


_last_played_rms: float | None = None
_last_played_time: float | None = None

# Whether a _respond() task's speak_stream() is actively playing audio right now,
# and when that playback started - services/brain/loop.py's barge-in hook reads
# this to decide whether a wake trigger is interrupting a real, audible response
# or just landing during silence (still waiting on the LLM/first synthesis, or
# nothing in flight at all). Only ever reflects _play_all() (real responses),
# not play_audio() (backchannel one-offs) - backchannels aren't behind a
# cancellable response_task, so there's nothing for barge-in to reason about there.
_response_playback_active = False
_response_playback_started_at: float | None = None


def _mark_playback_started() -> None:
    global _response_playback_active, _response_playback_started_at
    _response_playback_active = True
    _response_playback_started_at = time.perf_counter()
    playback_state.mark_started()  # PROMPTS.md A11 - the cross-process signal services/daemon/daemon.py reads


def _mark_playback_stopped() -> None:
    global _response_playback_active, _response_playback_started_at
    _response_playback_active = False
    _response_playback_started_at = None
    playback_state.mark_stopped()


def response_playback_elapsed_s() -> float | None:
    """Seconds since the current response's audio actually started playing, or
    None if nothing is playing right now. Deliberately measured from first audio
    out (ttfc), not from when the response task was created - a task that's still
    waiting on the LLM or on synthesis hasn't played anything yet, so there's
    nothing for a wake trigger to be interrupting."""
    if not _response_playback_active or _response_playback_started_at is None:
        return None
    return time.perf_counter() - _response_playback_started_at


def close() -> None:
    global _engine, _engine_name, _last_played_rms, _last_played_time
    if _engine is not None:
        _engine.close()
    _engine = None
    _engine_name = None
    _last_played_rms = None
    _last_played_time = None
    _mark_playback_stopped()


def _log(record: dict) -> None:
    VOICE_LOG_PATH.parent.mkdir(exist_ok=True)
    with VOICE_LOG_PATH.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}) + "\n")


def _rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0


def _ramp_gain(natural_rms: float, config: dict) -> float:
    """Caps how much louder this utterance can be than the last one actually
    played, if it follows within [voice.level_ramp].window_s - real conversation
    doesn't jump straight from a quiet backchannel to a full-volume response, it
    drifts back up over a couple of turns (CLAUDE.md's volume-continuity finding).
    Only ever attenuates toward continuity, never boosts - an utterance that's
    already quieter than the cap plays at its natural level unchanged."""
    ramp_cfg = config.get("level_ramp", {})
    if not ramp_cfg.get("enabled", True) or natural_rms <= 0:
        return 1.0
    if _last_played_rms is None or _last_played_time is None:
        return 1.0
    window_s = ramp_cfg.get("window_s", 6.0)
    if time.perf_counter() - _last_played_time > window_s:
        return 1.0
    step_db = ramp_cfg.get("step_db", 6.0)
    cap_rms = _last_played_rms * (10 ** (step_db / 20))
    if natural_rms <= cap_rms:
        return 1.0
    return cap_rms / natural_rms


def _record_played_level(rms: float) -> None:
    global _last_played_rms, _last_played_time
    if rms <= 0:
        return
    _last_played_rms = rms
    _last_played_time = time.perf_counter()


def _output_gain(config: dict) -> float:
    """Master output level - [voice].output_gain_db, one place covering every
    playback path (play_audio, _play_all - backchannels and real responses
    alike), not a per-strategy or per-caller setting. Applied after the ramp
    gain, not before: the ramp reasons about relative levels between utterances,
    and _record_played_level() tracks that pre-master-gain level so ramp
    comparisons stay in their own frame regardless of what the master is set to -
    this is purely a final multiply on top, with no feedback into ramp state."""
    db = config.get("output_gain_db", 0.0)
    return 10 ** (db / 20) if db != 0.0 else 1.0


_PLAYBACK_SUBBLOCK_S = 0.1  # ~100ms - keeps any single blocking stream.write() call
# short, regardless of how long the audio chunk being played actually is. Found
# empirically (not theoretical): stream.abort() does not reliably preempt an
# in-flight OutputStream.write() call - cancelling mid-write on a large chunk
# (e.g. buffered_stream's first ~sentence-1+2 call) could leave the worker thread
# genuinely stuck inside PortAudio's write(), which later crashed the process
# (access violation) when asyncio's executor tried to join that orphaned thread at
# interpreter shutdown. Writing in ~100ms sub-blocks bounds the worst case to about
# that long regardless of whether abort() actually works, and gives asyncio a real
# cancellation checkpoint between sub-blocks instead of one for the whole chunk.


async def _write_interruptible(stream: sd.OutputStream, audio: np.ndarray, sample_rate: int) -> None:
    subblock_samples = max(1, int(sample_rate * _PLAYBACK_SUBBLOCK_S))
    try:
        for start in range(0, len(audio), subblock_samples):
            subblock = audio[start:start + subblock_samples]
            await asyncio.to_thread(stream.write, subblock)
            # PROMPTS.md A15 - live lip-sync amplitude, updated at the same
            # ~100ms sub-block granularity this function already writes audio
            # at, not a separate timer. Both real callers of this shared
            # primitive (play_audio() and _play_all()) get it for free.
            playback_state.update_amplitude(_rms(subblock))
    finally:
        # play_audio() (backchannels) never calls playback_state.mark_stopped() -
        # only _play_all() does, for its own active/started_at tracking - so
        # without this, a backchannel's last sub-block amplitude would stick
        # after playback actually ends instead of returning to a closed mouth.
        # Reset lives here, in the one shared primitive, rather than duplicated
        # (and easy to forget) in each caller.
        playback_state.update_amplitude(0.0)


async def play_audio(audio: np.ndarray, sample_rate: int) -> None:
    """One-off single-clip playback (backchannel lines). Uses the same
    OutputStream + _write_interruptible() primitive as _play_all() - originally
    this used sd.play()+sd.wait()+sd.stop(), but that has the identical hang risk
    _write_interruptible was built for: reproduced directly, sd.stop() did not
    reliably interrupt a blocking sd.wait() either, and the process crashed the
    same way at shutdown. One safe playback primitive, not two."""
    config = _load_config()
    ramp_gain = _ramp_gain(_rms(audio), config)
    ramped = audio * ramp_gain if ramp_gain != 1.0 else audio
    output_gain = _output_gain(config)
    played = ramped * output_gain if output_gain != 1.0 else ramped

    stream = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32")
    stream.start()
    try:
        await _write_interruptible(stream, played, sample_rate)
    except asyncio.CancelledError:
        stream.abort()
        raise
    finally:
        await asyncio.to_thread(stream.stop)
        stream.close()
    _record_played_level(_rms(ramped))


def _split_sentences(buffer: str) -> tuple[list[str], str]:
    """Pull complete sentences off the front of buffer. Returns (sentences, remainder)."""
    sentences = []
    while True:
        match = _SENTENCE_END_RE.search(buffer)
        if match is None:
            break
        sentences.append(buffer[:match.end()])
        buffer = buffer[match.end():]
    return sentences, buffer


async def _consume_per_sentence(token_iterator: AsyncIterator[str], chunk_queue: "asyncio.Queue[str | None]") -> None:
    """Every sentence is its own synthesis unit, the instant it completes. Fastest
    time-to-first-audio, but XTTS conditions each call on isolated text - no
    following-sentence context - which measurably changes delivery character, not
    just introduces gaps (see CLAUDE.md's path-divergence investigation)."""
    buffer = ""
    async for token in token_iterator:
        buffer += token
        sentences, buffer = _split_sentences(buffer)
        for sentence in sentences:
            await chunk_queue.put(sentence)
    if buffer.strip():
        await chunk_queue.put(buffer)
    await chunk_queue.put(None)


async def _consume_whole_text(token_iterator: AsyncIterator[str], chunk_queue: "asyncio.Queue[str | None]") -> None:
    """The entire response as one synthesis call - best delivery character (XTTS
    conditions on the complete text), but zero streaming: no audio until the full
    response has been generated. For comparison/short-response use, not the default
    for anything long enough that time-to-first-audio matters. Split at _MAX_CHUNK_CHARS
    via _normalized_capped_chunks on the way out - a long response fed as one call was
    exactly the shape that reproducibly truncated past ~450 chars (see CLAUDE.md)."""
    buffer = ""
    async for token in token_iterator:
        buffer += token
    if buffer.strip():
        for piece in _normalized_capped_chunks(buffer, _MAX_CHUNK_CHARS):
            await chunk_queue.put(piece)
    await chunk_queue.put(None)


async def _consume_hybrid(token_iterator: AsyncIterator[str], chunk_queue: "asyncio.Queue[str | None]") -> None:
    """Sentence 1 alone (fast time-to-first-audio, same as per_sentence), then
    everything after buffered until the stream ends and sent as one whole-text call
    (delivery character closer to whole_text for the remainder, which is most of the
    response by word count in a typical multi-sentence reply). The remainder is
    split at _MAX_CHUNK_CHARS via _normalized_capped_chunks - on a long response
    that "everything after" call was exactly the shape that reproducibly truncated
    past ~450 chars (see CLAUDE.md)."""
    buffer = ""
    first_sentence_sent = False
    async for token in token_iterator:
        buffer += token
        if not first_sentence_sent:
            sentences, buffer = _split_sentences(buffer)
            if sentences:
                for piece in _normalized_capped_chunks(sentences[0], _MAX_CHUNK_CHARS):
                    await chunk_queue.put(piece)
                first_sentence_sent = True
                if len(sentences) > 1:
                    buffer = "".join(sentences[1:]) + buffer
    if buffer.strip():
        for piece in _normalized_capped_chunks(buffer, _MAX_CHUNK_CHARS):
            await chunk_queue.put(piece)
    await chunk_queue.put(None)


async def _consume_hybrid3(token_iterator: AsyncIterator[str], chunk_queue: "asyncio.Queue[str | None]") -> None:
    """Three-way split: sentence 1 alone, sentences 2-3 together, everything after
    as a final call once the stream ends. Fallback for when hybrid's 2-way split
    leaves too large a gap between sentence 1's playback and the remainder's
    synthesis - measured 3.1-3.6s on a realistic 4-sentence response (sentence 1's
    ~0.8s playback covering only a fraction of the ~3.8-4s it took to synthesize the
    other three sentences as one call). Splitting off a second chunk gives synthesis
    an earlier midpoint to catch up at, at the cost of one more character-shift
    boundary than hybrid. Chunk 2 and the remainder are both split at
    _MAX_CHUNK_CHARS via _normalized_capped_chunks - either can exceed it on a long
    enough response, the same truncation risk as hybrid's remainder (see CLAUDE.md)."""
    buffer = ""
    collected: list[str] = []
    stage = 0  # 0: need 1 sentence for chunk 1, 1: need 2 more for chunk 2, 2: remainder only
    async for token in token_iterator:
        buffer += token
        if stage < 2:
            sentences, buffer = _split_sentences(buffer)
            for i, sentence in enumerate(sentences):
                collected.append(sentence)
                if stage == 0 and len(collected) == 1:
                    for piece in _normalized_capped_chunks(collected[0], _MAX_CHUNK_CHARS):
                        await chunk_queue.put(piece)
                    collected = []
                    stage = 1
                elif stage == 1 and len(collected) == 2:
                    for piece in _normalized_capped_chunks("".join(collected), _MAX_CHUNK_CHARS):
                        await chunk_queue.put(piece)
                    collected = []
                    stage = 2
                    leftover = sentences[i + 1:]
                    if leftover:
                        buffer = "".join(leftover) + buffer
                    break
    if buffer.strip():
        for piece in _normalized_capped_chunks(buffer, _MAX_CHUNK_CHARS):
            await chunk_queue.put(piece)
    await chunk_queue.put(None)


_BUFFERED_START_CHAR_THRESHOLD = 250  # pure safety net - only fires if sentence 1 itself
# hasn't hit a sentence boundary by this many raw characters (an unusually long run-on
# opener), never the normal trigger. A5: the trigger is 1-sentence-alone now (the char
# threshold used to co-trigger at 150 chars, tested and found the resulting 118ms gap
# inaudible - see _consume_buffered_start's docstring) - raised well above any realistic
# single-sentence length (persona.md's short-opener rule keeps sentence 1 well under this)
# so it stays a backstop, not a second active trigger path.
_MAX_CHUNK_CHARS = 350  # hard per-call cap, used by every consumer that can combine multiple
# sentences into one synthesis call (whole_text, hybrid's remainder, hybrid3's chunk 2/3,
# buffered_stream). XTTS reproducibly truncated audio past ~450 characters in a single call
# (4/4 runs, always cutting the last word) while 357 chars came through clean every time -
# see CLAUDE.md. 350 keeps a margin under that boundary rather than riding right up against it.


def _split_into_capped_chunks(text: str, max_chars: int) -> list[str]:
    """Splits text into pieces of at most max_chars, preferring sentence boundaries
    so the safety cap doesn't cut mid-sentence - unless a single sentence itself
    exceeds max_chars, which gets hard-cut as a last resort rather than ever
    exceeding the cap (better than risking XTTS's truncation failure mode)."""
    sentences, remainder = _split_sentences(text)
    if remainder.strip():
        sentences = sentences + [remainder]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(sentence[i:i + max_chars] for i in range(0, len(sentence), max_chars))
            continue
        if len(current) + len(sentence) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        chunks.append(current)
    return chunks


def _normalized_capped_chunks(text: str, max_chars: int) -> list[str]:
    """sanitize() + normalize() BEFORE capping, not after. normalize() expands text
    - "1.2mm" becomes "one point two millimeters", 5 chars to 30 - so capping the
    raw streamed text first and normalizing each already-capped piece afterward (as
    _stream_synthesize()/_synthesize_all() do per-chunk, for chunks that arrive
    already clean from here) let a normalized chunk balloon back past max_chars and
    XTTS's ~402-token hard limit. That's what produced the "text length exceeds...
    250" warnings and the audio cutouts/distortion in the live A4 test - every
    _split_into_capped_chunks() call site upstream (whole_text, hybrid's remainder,
    hybrid3's chunk 2/3, buffered_stream's first chunk and remainder) had this bug,
    not just one strategy. _stream_synthesize()/_synthesize_all() still call
    sanitize()/normalize() on what they dequeue - harmless no-ops on text that's
    already clean, but still needed for _consume_per_sentence, whose chunks reach
    the queue raw."""
    clean = normalize(sanitize(text)) if text.strip() else ""
    return _split_into_capped_chunks(clean, max_chars) if clean else []


async def _consume_buffered_start(token_iterator: AsyncIterator[str], chunk_queue: "asyncio.Queue[str | None]") -> None:
    """First inference_stream call gets sentence 1 alone - the tightest trigger
    tried (A5). This is NOT A3's hybrid trade-off: hybrid fires one sentence then
    waits for the *entire rest of the response* as one call (that's what produced
    a 3.6s mid-response gap there) - here the remainder always fires as its own
    separate inference_stream() call regardless of how the first chunk triggers,
    so a tighter first-chunk trigger doesn't reopen that failure mode. Measured
    directly (scripts/compare_buffered_triggers.py, real speak_stream() runs):
    2-sentences-or-300-chars was 572ms TTFA/0ms max gap; 1-sentence-or-150-chars
    was 394ms/0ms; 1-sentence-alone was fastest (301ms) with a small real gap
    (118ms) - listening verdict: inaudible, landed as the default.
    _BUFFERED_START_CHAR_THRESHOLD is a pure safety net now (see its own comment),
    not a co-equal trigger - sentence completion is the only path expected to
    fire in normal use. Both pieces run through _normalized_capped_chunks
    (sanitize+normalize, THEN cap - see its docstring for why the order matters)
    so neither risks XTTS's long-input truncation."""
    buffer = ""
    first_chunk_sent = False
    async for token in token_iterator:
        buffer += token
        if not first_chunk_sent:
            sentences, remainder = _split_sentences(buffer)
            if len(sentences) >= 1:
                first_text = sentences[0]
                for piece in _normalized_capped_chunks(first_text, _MAX_CHUNK_CHARS):
                    await chunk_queue.put(piece)
                buffer = "".join(sentences[1:]) + remainder
                first_chunk_sent = True
            elif len(buffer) >= _BUFFERED_START_CHAR_THRESHOLD:
                for piece in _normalized_capped_chunks(buffer, _MAX_CHUNK_CHARS):
                    await chunk_queue.put(piece)
                buffer = ""
                first_chunk_sent = True
    if buffer.strip():
        for piece in _normalized_capped_chunks(buffer, _MAX_CHUNK_CHARS):
            await chunk_queue.put(piece)
    await chunk_queue.put(None)


_CONSUMERS = {
    "per_sentence": _consume_per_sentence,
    "whole_text": _consume_whole_text,
    "hybrid": _consume_hybrid,
    "hybrid3": _consume_hybrid3,
}

_STREAM_CONSUMERS = {
    "inference_stream": _consume_whole_text,
    "buffered_stream": _consume_buffered_start,
}


async def speak_stream(
    token_iterator: AsyncIterator[str], synth_workers: int | None = None, strategy: str | None = None,
) -> None:
    """Consume an async stream of LLM tokens, synthesizing and playing chunks as
    they're ready. How tokens get grouped into synthesis units is controlled by
    strategy (default from [voice].strategy): "per_sentence" (fastest first audio,
    each sentence isolated), "whole_text" (best delivery character, no streaming -
    waits for the full response), "hybrid" (sentence 1 alone for fast first audio,
    everything after as one call once the stream ends), "hybrid3" (sentence 1, then
    sentences 2-3, then the remainder), "inference_stream" (whole-text conditioning
    AND progressive output in one call, via the engine's synthesize_stream() if it
    has one - see xtts_engine.py; falls back to per_sentence if the active engine
    doesn't support it). Chunk N+1's tokens keep accumulating in the background
    while chunk N is synthesizing/playing, regardless of strategy - the full
    response is never waited on except by whole_text's inherent design.

    Three concurrent stages, not two - a queue between each: tokens -> chunks
    (strategy-specific consumer above), chunks -> audio (_synthesize_all), audio ->
    speakers (_play_all). Synthesis of chunk N+1 starts the instant a synthesis
    worker is free, not when playback of chunk N finishes - otherwise a slow engine
    would always show a gap between chunks equal to its own synthesis time,
    independent of whether it could actually keep up. gap_ms on each "sentence" log
    record is how long playback sat waiting on the audio queue: ~0 means synthesis
    was already ahead, positive means it wasn't and there was an audible stall.

    synth_workers (default from [voice].synth_workers, else 1): number of chunks
    synthesized concurrently. >1 lets chunk N+1's synthesis overlap chunk N's
    instead of queuing behind it - output order is still preserved (workers tag each
    result with its sequence index; _synthesize_all buffers early arrivals and only
    releases to the audio queue in order)."""
    engine, engine_name = _get_engine()
    config = _load_config()
    if synth_workers is None:
        synth_workers = config.get("synth_workers", 1)
    if strategy is None:
        strategy = config.get("strategy", "per_sentence")
    if strategy not in _CONSUMERS and strategy not in _STREAM_CONSUMERS:
        raise ValueError(
            f"Unknown [voice].strategy: {strategy!r} - expected one of {[*_CONSUMERS, *_STREAM_CONSUMERS]}"
        )
    if strategy in _STREAM_CONSUMERS and type(engine).synthesize_stream is TTSEngine.synthesize_stream:
        # Engine doesn't override synthesize_stream (e.g. Kokoro) - fall back rather
        # than ever triggering its NotImplementedError in practice.
        strategy = "per_sentence"

    chunk_queue: asyncio.Queue[str | None] = asyncio.Queue()
    audio_queue: asyncio.Queue[tuple[np.ndarray, dict] | None] = asyncio.Queue()
    stream_start = time.perf_counter()

    async def _stream_synthesize() -> None:
        """inference_stream's synthesis stage: one whole-text chunk in from
        _consume_whole_text, many audio chunks out - no chunk_queue reordering
        needed (there's only ever one text unit, and XTTS yields its audio chunks
        already in order)."""
        chunk_index = 0
        while True:
            text = await chunk_queue.get()
            if text is None:
                break
            clean = sanitize(text)
            clean = normalize(clean) if clean else clean
            if not clean:
                continue
            text_chars = len(clean)
            # Every synthesis call, its exact text and length - not derived from
            # the "sentence"/ttfc records below, which log playback timing per
            # audio sub-chunk, not the source text. Needed to point at the
            # specific chunk behind a live quality issue (e.g. accent drift)
            # instead of guessing from length alone.
            # since_stream_start_ms, measured the same way ttfc_ms is (relative to
            # stream_start, captured once at speak_stream()'s entry): for the
            # first chunk this is how long buffered_stream/hybrid/etc. waited for
            # enough LLM text to trigger synthesis at all - latency_report.py
            # subtracts it from ttfc_ms to get real engine synthesis time,
            # instead of misattributing LLM generation pacing to "TTS first chunk".
            _log({
                "stage": "synthesize_call", "engine": engine_name, "text": clean, "chars": text_chars,
                "since_stream_start_ms": round((time.perf_counter() - stream_start) * 1000, 1),
            })
            last_chunk_time = time.perf_counter()
            async for audio in engine.synthesize_stream(clean):
                now = time.perf_counter()
                chunk_synth_ms = (now - last_chunk_time) * 1000
                last_chunk_time = now
                if audio.size == 0:
                    continue
                chunk_index += 1
                await audio_queue.put((audio, {
                    "index": chunk_index, "chars": text_chars if chunk_index == 1 else 0,
                    "synth_ms": round(chunk_synth_ms, 1),
                }))
        await audio_queue.put(None)

    async def _synthesize_all() -> None:
        indexed_queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
        completion_queue: asyncio.Queue[tuple[int, np.ndarray, float, int] | None] = asyncio.Queue()

        async def _feed_workers() -> None:
            idx = 0
            while True:
                chunk = await chunk_queue.get()
                if chunk is None:
                    break
                clean = sanitize(chunk)
                clean = normalize(clean) if clean else clean
                if clean:
                    idx += 1
                    await indexed_queue.put((idx, clean))
            for _ in range(synth_workers):
                await indexed_queue.put(None)

        async def _worker() -> None:
            while True:
                item = await indexed_queue.get()
                if item is None:
                    break
                idx, clean = item
                # Every synthesis call, its exact text and length - see the same
                # log call in _stream_synthesize() for why this is separate from
                # the "sentence"/ttfc playback-timing records below. See
                # _stream_synthesize()'s matching comment for why
                # since_stream_start_ms is here too.
                _log({
                    "stage": "synthesize_call", "engine": engine_name, "text": clean, "chars": len(clean),
                    "since_stream_start_ms": round((time.perf_counter() - stream_start) * 1000, 1),
                })
                synth_start = time.perf_counter()
                audio = await asyncio.to_thread(engine.synthesize, clean)
                synth_ms = (time.perf_counter() - synth_start) * 1000
                await completion_queue.put((idx, audio, synth_ms, len(clean)))
            await completion_queue.put(None)

        async def _emit_ready(pending: dict, next_needed: int) -> int:
            while next_needed in pending:
                audio, synth_ms, chars = pending.pop(next_needed)
                if audio.size > 0:
                    await audio_queue.put((audio, {
                        "index": next_needed, "chars": chars, "synth_ms": round(synth_ms, 1),
                    }))
                next_needed += 1
            return next_needed

        async def _reorder() -> None:
            pending: dict[int, tuple[np.ndarray, float, int]] = {}
            next_needed = 1
            workers_done = 0
            while workers_done < synth_workers:
                item = await completion_queue.get()
                if item is None:
                    workers_done += 1
                    continue
                idx, audio, synth_ms, chars = item
                pending[idx] = (audio, synth_ms, chars)
                next_needed = await _emit_ready(pending, next_needed)
            next_needed = await _emit_ready(pending, next_needed)  # workers can finish out of order
            await audio_queue.put(None)

        await asyncio.gather(_feed_workers(), *[_worker() for _ in range(synth_workers)], _reorder())

    async def _play_all() -> None:
        # One persistent stream for the whole response, not a play_audio() call per
        # chunk - measured separate sd.play()+sd.wait() calls at ~60-80ms of stream
        # setup/teardown overhead each, on top of the audio's own duration. That's
        # real dead air at every chunk boundary regardless of queue timing (gap_ms
        # below only measures whether the next chunk was *ready* in time, not
        # whether the hardware stream itself stayed continuous). A persistent
        # OutputStream cut that to ~10-22ms. play_audio() itself is unchanged and
        # still right for one-off single-clip playback (backchannel lines).
        first_chunk = True
        last_playback_end: float | None = None
        ramp_gain = 1.0
        output_gain = _output_gain(config)  # constant for the whole call - a fixed
        # [voice].output_gain_db setting, not something that varies chunk to chunk.
        sum_sq = 0.0
        total_samples = 0
        stream = sd.OutputStream(samplerate=engine.sample_rate, channels=1, dtype="float32")
        stream.start()
        try:
            while True:
                item = await audio_queue.get()
                dequeue_end = time.perf_counter()  # after the blocking wait, not before - that's the point
                if item is None:
                    break
                audio, meta = item
                gap_ms = (dequeue_end - last_playback_end) * 1000 if last_playback_end is not None else None
                if first_chunk:
                    ttfc_ms = (time.perf_counter() - stream_start) * 1000
                    _log({"stage": "ttfc", "engine": engine_name, "ttfc_ms": round(ttfc_ms, 1)})
                    # Computed once from the first chunk and held for the whole
                    # utterance - recomputing per chunk would make the level pump
                    # up/down mid-response instead of ramping smoothly turn to turn.
                    ramp_gain = _ramp_gain(_rms(audio), config)
                    first_chunk = False
                    _mark_playback_started()
                ramped = audio * ramp_gain if ramp_gain != 1.0 else audio
                sum_sq += float(np.sum(np.square(ramped)))
                total_samples += len(ramped)
                played = ramped * output_gain if output_gain != 1.0 else ramped
                _log({
                    "stage": "sentence", "engine": engine_name, **meta,
                    "audio_s": round(len(audio) / engine.sample_rate, 3),
                    "gap_ms": round(gap_ms, 1) if gap_ms is not None else None,
                    "ramp_gain_db": round(20 * np.log10(ramp_gain), 1) if ramp_gain != 1.0 else None,
                    "output_gain_db": config.get("output_gain_db", 0.0) if output_gain != 1.0 else None,
                })
                try:
                    await _write_interruptible(stream, played, engine.sample_rate)
                except asyncio.CancelledError:
                    # Barge-in: abort() drops whatever's still buffered immediately,
                    # unlike stop() which finishes playing it out first. Best-effort
                    # on top of _write_interruptible's sub-blocking, which is what
                    # actually bounds how long this can take to land.
                    stream.abort()
                    raise
                last_playback_end = time.perf_counter()
            if total_samples > 0:
                _record_played_level((sum_sq / total_samples) ** 0.5)
        finally:
            _mark_playback_stopped()
            await asyncio.to_thread(stream.stop)
            stream.close()

    if strategy in _STREAM_CONSUMERS:
        consume = _STREAM_CONSUMERS[strategy]
        await asyncio.gather(consume(token_iterator, chunk_queue), _stream_synthesize(), _play_all())
    else:
        consume = _CONSUMERS[strategy]
        await asyncio.gather(consume(token_iterator, chunk_queue), _synthesize_all(), _play_all())


async def speak(text: str) -> None:
    """Synthesize and play one complete string. Convenience wrapper around
    speak_stream for callers that already have the full text."""
    async def _one() -> AsyncIterator[str]:
        yield text
    await speak_stream(_one())


async def _main() -> None:
    import sys
    text = " ".join(sys.argv[1:]) or (
        "Hello. This is a streaming test of the Cortana voice pipeline, "
        "spoken one sentence at a time as it arrives."
    )
    print(f"> {text}\n")
    await speak(text)
    print(f"(logged to {VOICE_LOG_PATH})")


if __name__ == "__main__":
    asyncio.run(_main())
