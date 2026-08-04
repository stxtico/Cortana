"""Generates status_update and correction both with and without
services/voice/normalize.py applied, so the effect can be judged by ear before
deciding whether to wire it into services/voice/tts.py's synthesis pipeline.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import soundfile as sf

from services.voice.normalize import normalize
from services.voice.xtts_engine import XTTSEngine

ROOT = Path(__file__).resolve().parent.parent
REF_PATH = ROOT / "voice_refs" / "calm" / "voice_ref_14.wav"
OUT_DIR = ROOT / "voice_refs" / "audition" / "normalization_test"

UTTERANCES = {
    "status_update": "The CAD job finished at 2:15. The STEP file exported clean.",
    "correction": "That wall's 1.2mm and your nozzle is 0.4. It'll delaminate.",
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Loading XTTS v2 (once)...")
    engine = XTTSEngine()
    engine.set_reference(str(REF_PATH))

    for label, text in UTTERANCES.items():
        normalized = normalize(text)
        print(f"\n{label}:")
        print(f"  raw:        {text!r}")
        print(f"  normalized: {normalized!r}")

        t0 = time.perf_counter()
        audio_raw = engine.synthesize(text)
        print(f"  raw synth={time.perf_counter()-t0:.2f}s")
        sf.write(OUT_DIR / f"{label}_raw.wav", audio_raw, engine.sample_rate)

        t0 = time.perf_counter()
        audio_norm = engine.synthesize(normalized)
        print(f"  normalized synth={time.perf_counter()-t0:.2f}s")
        sf.write(OUT_DIR / f"{label}_normalized.wav", audio_norm, engine.sample_rate)

    engine.close()
    print(f"\nDone. Compare *_raw.wav vs *_normalized.wav in {OUT_DIR}")


if __name__ == "__main__":
    main()
