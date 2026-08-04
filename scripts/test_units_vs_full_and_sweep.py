"""Two follow-up comparisons after units_only scored best in isolation:
1. units_only vs full normalization, same reference/params - is the "and decimals"
   combination (or just density of spelled-out numbers) what makes full sound worse?
2. units_only text across the temperature x speed sweep, reference held at calm_14 -
   units_only still sounds slightly robotic and normalization is ruled out as the
   cause there, so this checks whether params close the gap on baseline XTTS quality.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import soundfile as sf

from services.voice.normalize import _UNIT_RE, _spell_unit, normalize
from services.voice.xtts_engine import XTTSEngine

ROOT = Path(__file__).resolve().parent.parent
REF_PATH = ROOT / "voice_refs" / "calm" / "voice_ref_14.wav"
OUT_DIR = ROOT / "voice_refs" / "audition" / "units_vs_full"
SWEEP_OUT_DIR = ROOT / "voice_refs" / "audition" / "units_only_sweep"

RAW = "That wall's 1.2mm and your nozzle is 0.4. It'll delaminate."
UNITS_ONLY = _UNIT_RE.sub(_spell_unit, RAW)
FULL = normalize(RAW)

DEFAULT_TEMPERATURE = 0.75
DEFAULT_SPEED = 1.0
TEMPERATURE_VALUES = [0.5, 0.65, 0.75, 0.9]
SPEED_VALUES = [0.85, 1.0, 1.15]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SWEEP_OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"units_only: {UNITS_ONLY!r}")
    print(f"full:       {FULL!r}")

    print("\nLoading XTTS v2 (once)...")
    engine = XTTSEngine()
    engine.set_reference(str(REF_PATH))

    print("\n=== units_only vs full (default params) ===")
    for label, text in [("units_only", UNITS_ONLY), ("full", FULL)]:
        t0 = time.perf_counter()
        audio = engine.synthesize(text)
        print(f"{label:12s} synth={time.perf_counter()-t0:.2f}s -> {label}.wav")
        sf.write(OUT_DIR / f"{label}.wav", audio, engine.sample_rate)

    print("\n=== units_only across temperature sweep (speed=1.0) ===")
    for temp in TEMPERATURE_VALUES:
        t0 = time.perf_counter()
        audio = engine.synthesize(UNITS_ONLY, temperature=temp, speed=DEFAULT_SPEED)
        fname = f"temp{temp:.2f}.wav"
        sf.write(SWEEP_OUT_DIR / fname, audio, engine.sample_rate)
        print(f"  temp={temp:.2f} synth={time.perf_counter()-t0:.2f}s -> {fname}")

    print("\n=== units_only across speed sweep (temperature=0.75 default) ===")
    for speed in SPEED_VALUES:
        t0 = time.perf_counter()
        audio = engine.synthesize(UNITS_ONLY, temperature=DEFAULT_TEMPERATURE, speed=speed)
        fname = f"speed{speed:.2f}.wav"
        sf.write(SWEEP_OUT_DIR / fname, audio, engine.sample_rate)
        print(f"  speed={speed:.2f} synth={time.perf_counter()-t0:.2f}s -> {fname}")

    engine.close()
    print(f"\nDone. {OUT_DIR} and {SWEEP_OUT_DIR}")


if __name__ == "__main__":
    main()
