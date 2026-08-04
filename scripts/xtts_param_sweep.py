"""Sweeps XTTS temperature and speed on status_update and multi_sentence, holding
the reference fixed (calm_14, current [voice.xtts] default) so what's heard is the
parameter effect, not reference variation. Two independent 1D sweeps (temperature @
speed=1.0, speed @ temperature=default), not a full cross product - keeps the file
count listenable. Winning values go in [voice.xtts] in cortana.toml, not hardcoded.
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
OUT_DIR = ROOT / "voice_refs" / "audition" / "param_sweep"

UTTERANCES = {
    "status_update": ["The CAD job finished at 2:15. The STEP file exported clean."],
    "multi_sentence": [
        "Got it.",
        "Your meeting's at three, and the CAD job finished about an hour ago.",
        "The export looks clean, no errors in the log, and the STEP file is ready for review.",
        "I'll flag it if anything changes before then.",
    ],
}

DEFAULT_TEMPERATURE = 0.75
DEFAULT_SPEED = 1.0
TEMPERATURE_VALUES = [0.5, 0.65, 0.75, 0.9]
SPEED_VALUES = [0.85, 1.0, 1.15]
SILENCE_GAP_S = 0.25


def synth_utterance(engine: XTTSEngine, sentences: list[str], **params) -> np.ndarray:
    if len(sentences) == 1:
        return engine.synthesize(sentences[0], **params)
    gap = np.zeros(int(SILENCE_GAP_S * engine.sample_rate), dtype=np.float32)
    parts = []
    for i, sentence in enumerate(sentences):
        parts.append(engine.synthesize(sentence, **params))
        if i < len(sentences) - 1:
            parts.append(gap)
    return np.concatenate(parts)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Loading XTTS v2 (once)...")
    engine = XTTSEngine()
    engine.set_reference(str(REF_PATH))

    print("\n=== Temperature sweep (speed=1.0) ===")
    for temp in TEMPERATURE_VALUES:
        for utt_label, sentences in UTTERANCES.items():
            t0 = time.perf_counter()
            audio = synth_utterance(engine, sentences, temperature=temp, speed=DEFAULT_SPEED)
            synth_s = time.perf_counter() - t0
            fname = f"temp{temp:.2f}_{utt_label}.wav"
            sf.write(OUT_DIR / fname, audio, engine.sample_rate)
            print(f"  temp={temp:.2f} {utt_label:16s} synth={synth_s:.2f}s audio_s={len(audio)/engine.sample_rate:.2f} -> {fname}")

    print("\n=== Speed sweep (temperature=0.75 default) ===")
    for speed in SPEED_VALUES:
        for utt_label, sentences in UTTERANCES.items():
            t0 = time.perf_counter()
            audio = synth_utterance(engine, sentences, temperature=DEFAULT_TEMPERATURE, speed=speed)
            synth_s = time.perf_counter() - t0
            fname = f"speed{speed:.2f}_{utt_label}.wav"
            sf.write(OUT_DIR / fname, audio, engine.sample_rate)
            print(f"  speed={speed:.2f} {utt_label:16s} synth={synth_s:.2f}s audio_s={len(audio)/engine.sample_rate:.2f} -> {fname}")

    engine.close()
    print(f"\nDone. Files in {OUT_DIR}")


if __name__ == "__main__":
    main()
