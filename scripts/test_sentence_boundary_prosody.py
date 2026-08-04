"""Generates multi_sentence two ways - per-sentence synthesized and concatenated
(what speak_stream() actually does for streaming) vs. one whole-text XTTS call - to
isolate how much of the robotic quality is the streaming architecture (prosody can't
carry across sentence boundaries when each sentence is a separate synthesize() call)
versus the reference itself.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import soundfile as sf

from services.voice.xtts_engine import XTTSEngine

ROOT = Path(__file__).resolve().parent.parent
REF_PATH = ROOT / "voice_refs" / "calm" / "voice_ref_14.wav"
OUT_DIR = ROOT / "voice_refs" / "audition" / "prosody_test"

SENTENCES = [
    "Got it.",
    "Your meeting's at three, and the CAD job finished about an hour ago.",
    "The export looks clean, no errors in the log, and the STEP file is ready for review.",
    "I'll flag it if anything changes before then.",
]
WHOLE_TEXT = " ".join(SENTENCES)
SILENCE_GAP_S = 0.25


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"whole text ({len(WHOLE_TEXT)} chars): {WHOLE_TEXT!r}")
    print("Loading XTTS v2 (once)...")
    engine = XTTSEngine()
    engine.set_reference(str(REF_PATH))

    print("\n--- per-sentence (concatenated, matches speak_stream() production behavior) ---")
    gap = np.zeros(int(SILENCE_GAP_S * engine.sample_rate), dtype=np.float32)
    parts = []
    t0 = time.perf_counter()
    for i, sentence in enumerate(SENTENCES):
        parts.append(engine.synthesize(sentence))
        if i < len(SENTENCES) - 1:
            parts.append(gap)
    per_sentence_audio = np.concatenate(parts)
    print(f"synth={time.perf_counter()-t0:.2f}s total, audio_s={len(per_sentence_audio)/engine.sample_rate:.2f}")
    sf.write(OUT_DIR / "per_sentence.wav", per_sentence_audio, engine.sample_rate)

    print("\n--- whole text (one XTTS call, enable_text_splitting off - under the ~250 char limit) ---")
    t0 = time.perf_counter()
    whole_audio = engine.synthesize(WHOLE_TEXT)
    print(f"synth={time.perf_counter()-t0:.2f}s, audio_s={len(whole_audio)/engine.sample_rate:.2f}")
    sf.write(OUT_DIR / "whole_text.wav", whole_audio, engine.sample_rate)

    engine.close()
    print(f"\nDone. Compare {OUT_DIR}/per_sentence.wav vs whole_text.wav")


if __name__ == "__main__":
    main()
