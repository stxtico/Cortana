"""Regenerates the backchannel pool with the listening-fix candidates and saves
each line as a numbered wav + manifest for a by-ear pass before the changes ship
(services/ears/backchannel_pool.py's get_pool() singleton already reads
[audio.backchannel]'s reference/speed/volume_db, so once volume_db is picked
there's no further code change needed):

1. Prompt rewritten to require real lexical words ("Right.", "Yeah.", "Got it.")
   and explicitly forbid non-lexical sounds ("Mm", "Hmm") - those have no real
   XTTS pronunciation and came out as long, strange vocalizations (1.9-2.2s for
   two letters vs 1.2-1.5s for the real words in the first pass).
2. "soft" reference instead of "calm" (the real-response default).
3. speed=0.88 instead of the 1.0 default.
4. Gain: synthesized once per line (dry, no gain baked in), then rendered at both
   -8dB and -14dB from the same underlying audio so the comparison isn't
   confounded by XTTS's own generation-to-generation variance.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import soundfile as sf

from services.ears.backchannel_pool import BackchannelPool, _apply_gain_db
from services.voice import tts as voice_tts

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "voice_refs" / "audition" / "backchannel_pool_v2"
GAIN_VARIANTS_DB = [-8.0, -14.0]


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for db in GAIN_VARIANTS_DB:
        (OUT_DIR / f"neg{abs(db):.0f}db").mkdir(parents=True, exist_ok=True)

    engine, _ = voice_tts._get_engine()
    print(f"Engine warm. Active reference before: {engine.active_reference!r}")

    # volume_db=0.0 here - we want the dry synthesized audio so both gain variants
    # below come from the exact same generation, not two separate XTTS calls.
    # min_size=0 forces a full regeneration from empty.
    pool = BackchannelPool(min_size=0, target_size=10, reference="soft", speed=0.88, volume_db=0.0)
    added = await pool.ensure_filled()
    print(f"Generated {added} lines (dry). Active reference after (should be back to before): {engine.active_reference!r}\n")

    manifest = []
    for i, entry in enumerate(pool._pool, start=1):
        dry_peak = float(abs(entry.audio).max()) if entry.audio.size else 0.0
        line = f"{i:02d} {entry.text!r} (dry peak={dry_peak:.3f}, {len(entry.audio) / entry.sample_rate:.2f}s)"
        manifest.append(line)
        print(f"  {line}")
        for db in GAIN_VARIANTS_DB:
            variant_audio = _apply_gain_db(entry.audio, db)
            out_path = OUT_DIR / f"neg{abs(db):.0f}db" / f"{i:02d}.wav"
            sf.write(out_path, variant_audio, entry.sample_rate)

    (OUT_DIR / "manifest.txt").write_text("\n".join(manifest), encoding="utf-8")
    print(f"\nSaved {len(pool._pool)} lines x {len(GAIN_VARIANTS_DB)} gain variants to {OUT_DIR}")
    print(f"Compare {OUT_DIR / 'neg8db'} vs {OUT_DIR / 'neg14db'}")


if __name__ == "__main__":
    asyncio.run(main())
