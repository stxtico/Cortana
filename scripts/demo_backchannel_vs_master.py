"""Backchannel-vs-master recheck: [audio.backchannel].volume_db and
[voice].output_gain_db stack additively in dB (both are plain multiplicative
gains applied in sequence), so a backchannel volume_db picked against a 0dB
master doesn't mean the same thing once output_gain_db=-30 is in the signal
path too. Synthesizes one backchannel line dry (soft reference, 0.88 speed, no
gain) once, then renders it at several relative volume_db candidates stacked on
top of the -30dB master, so the actual final played level for each candidate is
what's being judged - not the relative attenuation in isolation.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import soundfile as sf

from services.ears.backchannel_pool import BackchannelPool
from services.voice import tts as voice_tts

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "voice_refs" / "audition" / "backchannel_vs_master"

MASTER_GAIN_DB = -30.0
# Candidates for [audio.backchannel].volume_db, re-evaluated against the new
# master - -8 (the old value) is kept in the set to confirm/refute "likely
# inaudible" directly rather than just asserting it.
CANDIDATE_RELATIVE_DB = [0.0, -2.0, -4.0, -6.0, -8.0]


def _rms_db(audio: np.ndarray) -> float:
    rms = voice_tts._rms(audio)
    return 20 * np.log10(rms) if rms > 0 else float("-inf")


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    engine, _ = voice_tts._get_engine()
    print(f"Engine warm. Generating one dry backchannel line (soft ref, speed=0.88, no gain)...")

    pool = BackchannelPool(min_size=0, target_size=1, reference="soft", speed=0.88, volume_db=0.0)
    await pool.ensure_filled()
    entry = pool._pool[0]
    dry_db = _rms_db(entry.audio)
    print(f"Line: {entry.text!r}, dry level: {dry_db:.1f}dB\n")

    print(f"Also rendering a normal response at the {MASTER_GAIN_DB}dB master for a level reference point...")
    response_text = "Got it. Your meeting's at three, and the export finished a few minutes ago."
    response_dry = await asyncio.to_thread(engine.synthesize, response_text)
    response_at_master = response_dry * (10 ** (MASTER_GAIN_DB / 20))
    sf.write(OUT_DIR / "response_at_master.wav", response_at_master, engine.sample_rate)
    print(f"  response_at_master.wav: {_rms_db(response_at_master):.1f}dB\n")

    manifest = [f"response_at_master.wav: {_rms_db(response_at_master):.1f}dB (normal response at output_gain_db={MASTER_GAIN_DB})"]
    for relative_db in CANDIDATE_RELATIVE_DB:
        total_db = MASTER_GAIN_DB + relative_db
        gain = 10 ** (total_db / 20)
        variant = entry.audio * gain
        out_path = OUT_DIR / f"volume_db_{relative_db:.0f}.wav"
        sf.write(out_path, variant, entry.sample_rate)
        line = (f"{out_path.name}: volume_db={relative_db:.0f} -> total {_rms_db(variant):.1f}dB "
                f"(master {MASTER_GAIN_DB} + backchannel {relative_db:.0f})")
        manifest.append(line)
        print(f"  {line}")

    (OUT_DIR / "manifest.txt").write_text("\n".join(manifest), encoding="utf-8")
    print(f"\nSaved to {OUT_DIR} - compare each volume_db_*.wav against response_at_master.wav")


if __name__ == "__main__":
    asyncio.run(main())
