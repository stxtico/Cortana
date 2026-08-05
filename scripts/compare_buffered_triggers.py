"""A5: compares buffered_stream trigger thresholds - NOT a repeat of A3's
hybrid-vs-hybrid3 comparison. hybrid fires one sentence then waits for the
*entire rest of the response* as a single call (that's what produced the 3.6s
mid-response gap in A3) - buffered_stream always fires its remainder as its own
separate inference_stream() call regardless of how the first chunk is triggered,
so tightening the first-chunk trigger here is a different experiment with a
different risk profile (smaller/less-conditioned first chunk, not a giant
unstreamed remainder).

Three variants of _consume_buffered_start's trigger condition, first-chunk
sentence-count and char-threshold-fallback made parameters instead of hardcoded
2-sentences-or-300-chars:
  - 1sentence            : fire on the first complete sentence (300-char fallback,
                            same safety net as production, rarely the actual trigger)
  - 1sentence_or_150chars: fire on 1 sentence OR 150 raw chars, whichever first
  - 2sentences_or_300chars: current production behavior (_consume_buffered_start
                            itself, unmodified - not a reimplementation)

Same harness pattern as compare_strategies.py: real speak_stream() run, capture
ttfc_ms/gap_ms from logs/voice.jsonl via a byte-offset bookmark, save + transcribe
the actual played audio (rule 6 - duration alone doesn't catch truncation).
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import sounddevice as sd
import soundfile as sf
import torch
import torchaudio

from services.ears.stt import Transcriber
from services.voice import tts

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "voice_refs" / "audition" / "buffered_trigger_comparison"

RESPONSE = (
    "Got it. "
    "Your meeting's at three, and the CAD job finished about an hour ago. "
    "The export looks clean, no errors in the log, and the STEP file is ready for review. "
    "I'll flag it if anything changes before then."
)
WORDS_PER_SEC = 80  # matches gemma4:e4b's real ~104 tok/s (see compare_strategies.py)
DELAY_S = 1 / WORDS_PER_SEC


async def realistic_tokens():
    words = RESPONSE.split(" ")
    for i, word in enumerate(words):
        suffix = "" if i == len(words) - 1 else " "
        yield word + suffix
        await asyncio.sleep(DELAY_S)


def _make_consumer(min_sentences: int, char_threshold: int):
    """Mirrors _consume_buffered_start's own logic exactly, with min_sentences/
    char_threshold as parameters instead of the hardcoded 2 / _BUFFERED_START_CHAR_THRESHOLD
    (300) - everything downstream (capping, normalization, remainder-as-its-own-call)
    is untouched production code (tts._normalized_capped_chunks, tts._split_sentences)."""
    async def _consume(token_iterator, chunk_queue):
        buffer = ""
        first_chunk_sent = False
        async for token in token_iterator:
            buffer += token
            if not first_chunk_sent:
                sentences, remainder = tts._split_sentences(buffer)
                if len(sentences) >= min_sentences:
                    first_text = "".join(sentences[:min_sentences])
                    for piece in tts._normalized_capped_chunks(first_text, tts._MAX_CHUNK_CHARS):
                        await chunk_queue.put(piece)
                    buffer = "".join(sentences[min_sentences:]) + remainder
                    first_chunk_sent = True
                elif len(buffer) >= char_threshold:
                    for piece in tts._normalized_capped_chunks(buffer, tts._MAX_CHUNK_CHARS):
                        await chunk_queue.put(piece)
                    buffer = ""
                    first_chunk_sent = True
        if buffer.strip():
            for piece in tts._normalized_capped_chunks(buffer, tts._MAX_CHUNK_CHARS):
                await chunk_queue.put(piece)
        await chunk_queue.put(None)
    return _consume


VARIANTS = {
    "1sentence": _make_consumer(1, 300),
    "1sentence_or_150chars": _make_consumer(1, 150),
    "2sentences_or_300chars_current": tts._consume_buffered_start,  # unmodified production function
}


def _read_new_log_lines(offset: int) -> list[dict]:
    if not tts.VOICE_LOG_PATH.exists():
        return []
    with tts.VOICE_LOG_PATH.open("r") as f:
        f.seek(offset)
        lines = f.readlines()
    return [json.loads(line) for line in lines if line.strip()]


async def run_variant(name: str, consumer, transcriber: Transcriber) -> dict:
    print(f"\n=== {name} ===")
    captured_chunks: list[np.ndarray] = []
    orig_write = sd.OutputStream.write

    def _capturing_write(self, data, *args, **kwargs):
        captured_chunks.append(np.copy(data))
        return orig_write(self, data, *args, **kwargs)

    original_consumer = tts._STREAM_CONSUMERS["buffered_stream"]
    tts._STREAM_CONSUMERS["buffered_stream"] = consumer

    offset = tts.VOICE_LOG_PATH.stat().st_size if tts.VOICE_LOG_PATH.exists() else 0
    sd.OutputStream.write = _capturing_write
    t_start = time.perf_counter()
    try:
        await tts.speak_stream(realistic_tokens(), strategy="buffered_stream")
    finally:
        sd.OutputStream.write = orig_write
        tts._STREAM_CONSUMERS["buffered_stream"] = original_consumer
    total_s = time.perf_counter() - t_start

    engine, _ = tts._get_engine()
    audio = np.concatenate(captured_chunks) if captured_chunks else np.zeros(0, dtype=np.float32)
    out_path = OUT_DIR / f"{name}.wav"
    sf.write(out_path, audio, engine.sample_rate)

    records = _read_new_log_lines(offset)
    ttfc_ms = next((r["ttfc_ms"] for r in records if r.get("stage") == "ttfc"), None)
    sentence_records = [r for r in records if r.get("stage") == "sentence"]
    gaps = [r["gap_ms"] for r in sentence_records if r.get("gap_ms") is not None]

    audio_16k = torchaudio.functional.resample(
        torch.from_numpy(audio), engine.sample_rate, 16000
    ).numpy() if audio.size else audio
    transcript = transcriber.transcribe(audio_16k)

    result = {
        "variant": name,
        "ttfc_ms": round(ttfc_ms, 1) if ttfc_ms is not None else None,
        "total_s": round(total_s, 2),
        "audio_s": round(len(audio) / engine.sample_rate, 2) if audio.size else 0.0,
        "n_chunks": len(sentence_records),
        "gaps_ms": [round(g, 1) for g in gaps],
        "max_gap_ms": round(max(gaps), 1) if gaps else None,
        "transcript": transcript.text,
        "wav": str(out_path),
    }
    print(f"  ttfc={result['ttfc_ms']}ms total={result['total_s']}s audio={result['audio_s']}s "
          f"chunks={result['n_chunks']} gaps={result['gaps_ms']} max_gap={result['max_gap_ms']}ms")
    print(f"  transcript: {transcript.text!r}")
    return result


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Warming engine...")
    await tts.speak("Warmup.")
    print("Engine warm.")

    print("Loading transcriber (large-v3-turbo, cuda, float16)...")
    transcriber = Transcriber(model_name="large-v3-turbo", device="cuda", compute_type="float16", language="en")

    print(f"\nSource text ({len(RESPONSE)} chars): {RESPONSE!r}")

    results = []
    for name, consumer in VARIANTS.items():
        result = await run_variant(name, consumer, transcriber)
        results.append(result)
        await asyncio.sleep(0.5)

    print("\n\n=== Summary ===")
    print(f"{'variant':<32} {'ttfc_ms':>9} {'total_s':>8} {'chunks':>7} {'max_gap_ms':>11}")
    for r in results:
        print(f"{r['variant']:<32} {str(r['ttfc_ms']):>9} {r['total_s']:>8} "
              f"{r['n_chunks']:>7} {str(r['max_gap_ms']):>11}")

    print("\n=== Transcript check (expect full RESPONSE text in each) ===")
    for r in results:
        print(f"{r['variant']:<32}: {r['transcript']!r}")

    summary_path = OUT_DIR / "results.json"
    summary_path.write_text(json.dumps(results, indent=2))
    print(f"\nSaved audio + results.json to {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
