"""Full comparison of all six [voice].strategy options on the same realistic
multi-sentence response: per_sentence, whole_text, hybrid, hybrid3,
inference_stream, buffered_stream.

For each strategy: runs a real speak_stream() with realistic word-paced tokens,
captures TTFC (time to first audio reaching the output stream) and per-chunk
gap_ms/synth_ms from logs/voice.jsonl (isolated per-run via a byte-offset
bookmark), records total wall time, saves the actual played audio to a wav file
(via a temporary sd.OutputStream.write monkeypatch that still calls through to
real playback - doesn't disturb the timing the gap measurements depend on), and
transcribes the saved audio to catch truncation directly rather than trusting
duration, per the char-limit investigation's lesson that duration alone can be
misleading.

No strategy is set as the config default here - that's the user's call after
listening to the saved files.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

import numpy as np
import sounddevice as sd
import soundfile as sf
import torchaudio
import torch

from services.voice import tts
from services.ears.stt import Transcriber

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "voice_refs" / "audition" / "strategy_comparison"

RESPONSE = (
    "Got it. "
    "Your meeting's at three, and the CAD job finished about an hour ago. "
    "The export looks clean, no errors in the log, and the STEP file is ready for review. "
    "I'll flag it if anything changes before then."
)
WORDS_PER_SEC = 80  # matches gemma4:e4b's real ~104 tok/s generation speed (see prior
# strategy test scripts, e.g. test_barge_in.py) - not a human speaking pace.
DELAY_S = 1 / WORDS_PER_SEC

STRATEGIES = ["per_sentence", "whole_text", "hybrid", "hybrid3", "inference_stream", "buffered_stream"]


async def realistic_tokens():
    words = RESPONSE.split(" ")
    for i, word in enumerate(words):
        suffix = "" if i == len(words) - 1 else " "
        yield word + suffix
        await asyncio.sleep(DELAY_S)


def _read_new_log_lines(offset: int) -> list[dict]:
    if not tts.VOICE_LOG_PATH.exists():
        return []
    with tts.VOICE_LOG_PATH.open("r") as f:
        f.seek(offset)
        lines = f.readlines()
    return [json.loads(line) for line in lines if line.strip()]


async def run_strategy(strategy: str, transcriber: Transcriber) -> dict:
    print(f"\n=== {strategy} ===")
    captured_chunks: list[np.ndarray] = []
    orig_write = sd.OutputStream.write

    def _capturing_write(self, data, *args, **kwargs):
        captured_chunks.append(np.copy(data))
        return orig_write(self, data, *args, **kwargs)

    offset = tts.VOICE_LOG_PATH.stat().st_size if tts.VOICE_LOG_PATH.exists() else 0

    sd.OutputStream.write = _capturing_write
    t_start = time.perf_counter()
    try:
        await tts.speak_stream(realistic_tokens(), strategy=strategy)
    finally:
        sd.OutputStream.write = orig_write
    total_s = time.perf_counter() - t_start

    engine, _ = tts._get_engine()
    audio = np.concatenate(captured_chunks) if captured_chunks else np.zeros(0, dtype=np.float32)
    out_path = OUT_DIR / f"{strategy}.wav"
    sf.write(out_path, audio, engine.sample_rate)

    records = _read_new_log_lines(offset)
    ttfc_ms = next((r["ttfc_ms"] for r in records if r.get("stage") == "ttfc"), None)
    sentence_records = [r for r in records if r.get("stage") == "sentence"]
    gaps = [r["gap_ms"] for r in sentence_records if r.get("gap_ms") is not None]

    # Transcribe at 16kHz (Whisper's expected rate) - resample from XTTS's 24kHz.
    audio_16k = torchaudio.functional.resample(
        torch.from_numpy(audio), engine.sample_rate, 16000
    ).numpy() if audio.size else audio
    transcript = transcriber.transcribe(audio_16k)

    result = {
        "strategy": strategy,
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
          f"chunks={result['n_chunks']} max_gap={result['max_gap_ms']}ms")
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
    for strategy in STRATEGIES:
        result = await run_strategy(strategy, transcriber)
        results.append(result)
        await asyncio.sleep(0.5)  # let the output stream fully close before the next run

    print("\n\n=== Summary ===")
    print(f"{'strategy':<18} {'ttfc_ms':>9} {'total_s':>8} {'audio_s':>8} {'chunks':>7} {'max_gap_ms':>11}")
    for r in results:
        print(f"{r['strategy']:<18} {str(r['ttfc_ms']):>9} {r['total_s']:>8} {r['audio_s']:>8} "
              f"{r['n_chunks']:>7} {str(r['max_gap_ms']):>11}")

    print("\n=== Transcript check (expect full RESPONSE text in each) ===")
    for r in results:
        print(f"{r['strategy']:<18}: {r['transcript']!r}")

    summary_path = OUT_DIR / "results.json"
    summary_path.write_text(json.dumps(results, indent=2))
    print(f"\nSaved audio + results.json to {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
