"""Diagnoses the "first sentence sounds British in the pipeline but not in
full.wav" report. Confirmed via direct inspection: reference (active_reference is
"calm", same file), inference params (identical to XTTSEngine's own defaults, which
cortana.toml's [voice.xtts] mirrors), and sanitize() (only strips the sentence-
splitter's trailing space, no textual change) all match between paths. The real
divergence: speak_stream()'s sentence-splitter breaks the correction text into two
separate synthesize() calls (matching real pipeline behavior), while
test_units_vs_full_and_sweep.py synthesized both sentences as one call (what
produced full.wav). The pipeline never gives XTTS the second sentence as context
when generating the first.

This generates 5 runs each way - isolated first sentence only (pipeline path) vs.
both sentences together (test-script/full.wav path) - to separate two hypotheses:
- within-path variance: does the isolated-sentence-only version vary run to run
  even at fixed input/reference/params? (sampling instability, not a path bug)
- between-path divergence: does isolated consistently differ from combined?
  (confirms sentence-splitting itself, not chance, drives the character shift)
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import soundfile as sf

from services.voice import tts
from services.voice.normalize import normalize
from services.voice.tts import sanitize
from services.voice.xtts_engine import XTTSEngine

ROOT = Path(__file__).resolve().parent.parent
REF_PATH = ROOT / "voice_refs" / "calm" / "voice_ref_14.wav"
OUT_DIR = ROOT / "voice_refs" / "audition" / "path_divergence"

RAW = "That wall's 1.2mm and your nozzle is 0.4. It'll delaminate."
REPEATS = 5


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    sentences, _ = tts._split_sentences(RAW + " ")
    isolated_first = normalize(sanitize(sentences[0]))
    combined = normalize(sanitize(RAW))
    print(f"isolated first sentence (pipeline path): {isolated_first!r}")
    print(f"combined both sentences (test-script/full.wav path): {combined!r}")

    print("\nLoading XTTS v2 (once)...")
    engine = XTTSEngine()
    engine.set_reference(str(REF_PATH))

    print(f"\n=== pipeline path: isolated first sentence x{REPEATS} ===")
    for i in range(REPEATS):
        t0 = time.perf_counter()
        audio = engine.synthesize(isolated_first)
        fname = f"pipeline_path_rep{i}.wav"
        sf.write(OUT_DIR / fname, audio, engine.sample_rate)
        print(f"  rep{i} synth={time.perf_counter()-t0:.2f}s audio_s={len(audio)/engine.sample_rate:.2f} -> {fname}")

    print(f"\n=== test-script path: both sentences combined x{REPEATS} ===")
    for i in range(REPEATS):
        t0 = time.perf_counter()
        audio = engine.synthesize(combined)
        fname = f"testscript_path_rep{i}.wav"
        sf.write(OUT_DIR / fname, audio, engine.sample_rate)
        print(f"  rep{i} synth={time.perf_counter()-t0:.2f}s audio_s={len(audio)/engine.sample_rate:.2f} -> {fname}")

    engine.close()
    print(f"\nDone. Files in {OUT_DIR}")
    print("Compare within pipeline_path_rep*.wav (stability), within testscript_path_rep*.wav")
    print("(stability), and pipeline_path_* vs testscript_path_* (the actual divergence).")


if __name__ == "__main__":
    main()
