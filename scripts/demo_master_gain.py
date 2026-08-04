"""Master output gain ([voice].output_gain_db) comparison: synthesizes one
realistic response dry (no ramp involved - this is a single isolated utterance,
nothing played immediately before it) and renders it at -10/-20/-30dB so the
three can be compared directly, then reports the exact played dB for each.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import soundfile as sf

from services.voice import tts as voice_tts

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "voice_refs" / "audition" / "master_gain_demo"

TEXT = "Got it. Your meeting's at three, and the export finished a few minutes ago, looking clean."
GAIN_VARIANTS_DB = [-10.0, -20.0, -30.0]


def _rms_db(audio: np.ndarray) -> float:
    rms = voice_tts._rms(audio)
    return 20 * np.log10(rms) if rms > 0 else float("-inf")


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    engine, _ = voice_tts._get_engine()
    print(f"Engine warm, reference={engine.active_reference!r}. Synthesizing dry (no gain)...")
    dry = await asyncio.to_thread(engine.synthesize, TEXT)
    dry_db = _rms_db(dry)
    print(f"Dry level: {dry_db:.1f}dB, {len(dry) / engine.sample_rate:.2f}s\n")

    sf.write(OUT_DIR / "dry_0db.wav", dry, engine.sample_rate)
    manifest = [f"dry_0db.wav: {dry_db:.1f}dB (no master gain, for reference)"]

    for db in GAIN_VARIANTS_DB:
        gain = 10 ** (db / 20)
        variant = dry * gain
        out_path = OUT_DIR / f"neg{abs(db):.0f}db.wav"
        sf.write(out_path, variant, engine.sample_rate)
        line = f"{out_path.name}: {_rms_db(variant):.1f}dB"
        manifest.append(line)
        print(f"  {line}")

    (OUT_DIR / "manifest.txt").write_text("\n".join(manifest), encoding="utf-8")
    print(f"\nSaved to {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
