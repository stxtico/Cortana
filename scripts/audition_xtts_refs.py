"""Generates the same test sentence from each shortlisted voice reference so the
picks can be compared by ear (PROMPTS.md A3 step 3, before switching [voice].engine).
Loads the XTTS model once and swaps the cached reference per candidate via
XTTSEngine.set_reference() - reloading the full model per candidate would cost
~10s each for no reason (rule 7).
"""

import sys
import time
from pathlib import Path

import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.voice.xtts_engine import XTTSEngine

ROOT = Path(__file__).resolve().parent.parent
TEST_SENTENCE = "Your meeting's at three, and the CAD job finished about an hour ago."
OUT_DIR = ROOT / "voice_refs" / "audition"

CANDIDATES = [
    ("snr_05", ROOT / "voice_refs" / "voice_ref_05.wav"),
    ("snr_09", ROOT / "voice_refs" / "voice_ref_09.wav"),
    ("snr_10", ROOT / "voice_refs" / "voice_ref_10.wav"),
    ("snr_11", ROOT / "voice_refs" / "voice_ref_11.wav"),
    ("snr_13", ROOT / "voice_refs" / "voice_ref_13.wav"),
    ("snr_14", ROOT / "voice_refs" / "voice_ref_14.wav"),
    ("calm_01", ROOT / "voice_refs" / "calm" / "voice_ref_01.wav"),
    ("calm_03", ROOT / "voice_refs" / "calm" / "voice_ref_03.wav"),
    ("calm_08", ROOT / "voice_refs" / "calm" / "voice_ref_08.wav"),
    ("calm_10", ROOT / "voice_refs" / "calm" / "voice_ref_10.wav"),
    ("calm_14", ROOT / "voice_refs" / "calm" / "voice_ref_14.wav"),
    ("calm_19", ROOT / "voice_refs" / "calm" / "voice_ref_19.wav"),
]


def main() -> None:
    for _, path in CANDIDATES:
        if not path.exists():
            raise SystemExit(f"Missing candidate reference: {path}")

    print("Loading XTTS v2 (once)...")
    t0 = time.perf_counter()
    engine = XTTSEngine()
    print(f"model load: {time.perf_counter() - t0:.1f}s\n")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for label, ref_path in CANDIDATES:
        t0 = time.perf_counter()
        engine.set_reference(str(ref_path))
        latent_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        audio = engine.synthesize(TEST_SENTENCE)
        synth_s = time.perf_counter() - t0

        out_path = OUT_DIR / f"{label}.wav"
        sf.write(out_path, audio, engine.sample_rate)
        print(f"{label:10s} latents={latent_s:.2f}s synth={synth_s:.2f}s -> {out_path.relative_to(ROOT)}")

    engine.close()
    print(f"\nDone. {len(CANDIDATES)} samples in {OUT_DIR.relative_to(ROOT)}, all saying: {TEST_SENTENCE!r}")


if __name__ == "__main__":
    main()
