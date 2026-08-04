"""Measures whether XTTS's 250-char/lang warning corresponds to real truncation or
is purely advisory. Source check (services/voice's investigation, see CLAUDE.md):
check_input_length() only logs a warning, never truncates or raises - the actual
hard stop is a 402 GPT-token assertion in inference()/inference_stream(), unrelated
to the character-count heuristic. This generates real text at ~100 (safe baseline)/
200/300/400/500 chars and compares produced audio duration against the chars/second
rate established by the baseline - if longer texts fall off that rate, something is
really cutting audio short; if not, the warning doesn't reflect an actual problem in
this range.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import soundfile as sf

from services.voice.xtts_engine import XTTSEngine

ROOT = Path(__file__).resolve().parent.parent
REF_PATH = ROOT / "voice_refs" / "calm" / "voice_ref_14.wav"
OUT_DIR = ROOT / "voice_refs" / "audition" / "char_limit_test"

# Real content, not filler repeated to hit a length - extended naturally so each
# length band reads as something a real response would actually say.
SAMPLES = {
    100: (
        "Got it. Your meeting's at three, and the CAD job finished about an hour ago."
    ),
    200: (
        "Got it. Your meeting's at three, and the CAD job finished about an hour ago. "
        "The export looks clean, no errors in the log, and the file's ready for review."
    ),
    300: (
        "Got it. Your meeting's at three, and the CAD job finished about an hour ago. "
        "The export looks clean, no errors in the log, and the file's ready for review. "
        "I'll flag it if anything changes before then, but so far everything checks out fine."
    ),
    400: (
        "Got it. Your meeting's at three, and the CAD job finished about an hour ago. "
        "The export looks clean, no errors in the log, and the file's ready for review. "
        "I'll flag it if anything changes before then, but so far everything checks out fine. "
        "The wall thickness came in a bit under spec, so you might want to double check the "
        "tolerances before sending it off."
    ),
    500: (
        "Got it. Your meeting's at three, and the CAD job finished about an hour ago. "
        "The export looks clean, no errors in the log, and the file's ready for review. "
        "I'll flag it if anything changes before then, but so far everything checks out fine. "
        "The wall thickness came in a bit under spec, so you might want to double check the "
        "tolerances before sending it off. Otherwise the geometry all looks solid, and the "
        "print estimate came back at just under three hours."
    ),
}


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    engine = XTTSEngine()
    engine.set_reference(str(REF_PATH))

    results = []
    for target_len, text in SAMPLES.items():
        actual_len = len(text)
        t0 = time.perf_counter()
        chunks = [chunk async for chunk in engine.synthesize_stream(text)]
        synth_s = time.perf_counter() - t0
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        audio_s = len(audio) / engine.sample_rate
        chars_per_audio_s = actual_len / audio_s if audio_s > 0 else 0
        results.append((target_len, actual_len, synth_s, audio_s, chars_per_audio_s))
        out_path = OUT_DIR / f"chars_{target_len}.wav"
        sf.write(out_path, audio, engine.sample_rate)
        print(f"target={target_len:4d} actual_chars={actual_len:4d} synth_s={synth_s:6.2f} "
              f"audio_s={audio_s:6.2f} chars/audio_s={chars_per_audio_s:5.1f} -> {out_path.name}")

    engine.close()

    baseline_rate = results[0][4]  # 100-char sample, well under any limit
    print(f"\nBaseline rate (100-char sample): {baseline_rate:.1f} chars/audio_s")
    print("If longer samples' rate drops well below this, audio is being cut short:")
    for target_len, actual_len, synth_s, audio_s, rate in results[1:]:
        pct_of_baseline = (rate / baseline_rate) * 100
        flag = "  <-- SUSPECT TRUNCATION" if pct_of_baseline < 85 else ""
        print(f"  {target_len:4d} chars: {rate:5.1f} chars/audio_s ({pct_of_baseline:.0f}% of baseline){flag}")


if __name__ == "__main__":
    asyncio.run(main())
