"""Isolates each normalize.py transform (times/decimals/units/integers) applied
alone to the raw correction utterance, to narrow down which one (if any) is
responsible for the "more British" quality reported on the fully-normalized output.
Neither of the two literal hypotheses checked out: num2words has no distinct en_US
locale in this installed version (CONVERTER_CLASSES only has en/en_IN/en_NG - en_US
silently falls back to the base en class, which is why passing lang="en_US" changed
nothing), and _UNIT_WORDS already uses American spellings. So this isolates
mechanically rather than assuming which piece is at fault.

Note: applying a transform truly alone (not through normalize()'s ordered pipeline)
can produce malformed intermediate text where transforms would normally interact -
e.g. integers-only on "1.2mm" matches the "1" and "4"/"0" as bare integers around a
decimal point, since _INT_RE's word-boundary logic doesn't know about the decimal
context normalize() would normally handle first. That's expected and reported as-is,
not hidden - the point is hearing what XTTS does with each isolated substitution.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import soundfile as sf
from num2words import num2words

from services.voice.normalize import _DECIMAL_RE, _INT_RE, _TIME_RE, _UNIT_RE, _spell_number, _spell_time, _spell_unit
from services.voice.xtts_engine import XTTSEngine

ROOT = Path(__file__).resolve().parent.parent
REF_PATH = ROOT / "voice_refs" / "calm" / "voice_ref_14.wav"
OUT_DIR = ROOT / "voice_refs" / "audition" / "normalization_isolation"

RAW = "That wall's 1.2mm and your nozzle is 0.4. It'll delaminate."

TRANSFORMS = {
    "times_only": lambda t: _TIME_RE.sub(_spell_time, t),
    "decimals_only": lambda t: _DECIMAL_RE.sub(lambda m: _spell_number(m.group(0)), t),
    "units_only": lambda t: _UNIT_RE.sub(_spell_unit, t),
    "integers_only": lambda t: _INT_RE.sub(lambda m: num2words(int(m.group(0))), t),
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"raw: {RAW!r}\n")
    variants = {"raw": RAW}
    for label, fn in TRANSFORMS.items():
        variants[label] = fn(RAW)
        print(f"{label:15s}: {variants[label]!r}")

    print("\nLoading XTTS v2 (once)...")
    engine = XTTSEngine()
    engine.set_reference(str(REF_PATH))

    print()
    for label, text in variants.items():
        t0 = time.perf_counter()
        audio = engine.synthesize(text)
        synth_s = time.perf_counter() - t0
        out_path = OUT_DIR / f"{label}.wav"
        sf.write(out_path, audio, engine.sample_rate)
        print(f"{label:15s} synth={synth_s:.2f}s -> {out_path.name}")

    engine.close()
    print(f"\nDone. Files in {OUT_DIR}")


if __name__ == "__main__":
    main()
