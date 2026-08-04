"""Demonstrates the volume-continuity level ramp (tts.py's _ramp_gain /
_record_played_level): play one quiet backchannel line, then three consecutive
short response turns immediately after, all real synthesis/playback through the
actual production paths (play_audio() and speak()/speak_stream()) - not a
synthetic gain calculation. Captures each turn's actual played audio (via the
same OutputStream/sd.play monkeypatch pattern used in compare_strategies.py) and
reports the real RMS/dB of each turn so the ramp is verifiable by number, not
just by ear - then saves everything, individually and as one concatenated
sequence, for a listen.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import sounddevice as sd
import soundfile as sf

from services.ears.backchannel_pool import BackchannelPool
from services.voice import tts as voice_tts


def _read_new_ramp_gains(offset: int) -> list[float | None]:
    if not voice_tts.VOICE_LOG_PATH.exists():
        return []
    with voice_tts.VOICE_LOG_PATH.open("r") as f:
        f.seek(offset)
        lines = f.readlines()
    records = [json.loads(ln) for ln in lines if ln.strip()]
    return [r.get("ramp_gain_db") for r in records if r.get("stage") == "sentence"]

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "voice_refs" / "audition" / "level_ramp_demo"

RESPONSE_TURNS = [
    "Yeah, that's the one - go ahead and use it.",
    "The export finished a few minutes ago, looks clean.",
    "No errors in the log, so you should be good to go.",
]


def _rms_db(audio: np.ndarray) -> float:
    rms = voice_tts._rms(audio)
    return 20 * np.log10(rms) if rms > 0 else float("-inf")


async def _capture(coro) -> np.ndarray:
    """Runs coro (a real play_audio()/speak() call) while capturing every array
    actually written to the audio device, in order - preserves real timing (the
    ramp's window_s check depends on wall-clock gaps between turns being real)."""
    chunks: list[np.ndarray] = []
    orig_stream_write = sd.OutputStream.write
    orig_play = sd.play

    def _capturing_write(self, data, *args, **kwargs):
        chunks.append(np.copy(data))
        return orig_stream_write(self, data, *args, **kwargs)

    def _capturing_play(data, *args, **kwargs):
        chunks.append(np.copy(data))
        return orig_play(data, *args, **kwargs)

    sd.OutputStream.write = _capturing_write
    sd.play = _capturing_play
    try:
        await coro
    finally:
        sd.OutputStream.write = orig_stream_write
        sd.play = orig_play
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Warming engine...")
    await voice_tts.speak("Warmup.")
    voice_tts._last_played_rms = None  # reset so warmup doesn't count as "the last thing played"
    voice_tts._last_played_time = None
    print("Engine warm.\n")

    pool = BackchannelPool(min_size=0, target_size=1, reference="soft", speed=0.88, volume_db=-8.0)
    await pool.ensure_filled()
    backchannel = pool._pool[0]
    print(f"Backchannel line: {backchannel.text!r}, natural rms={_rms_db(backchannel.audio):.1f}dB")

    sample_rate = voice_tts._get_engine()[0].sample_rate
    segments = []

    audio = await _capture(voice_tts.play_audio(backchannel.audio, backchannel.sample_rate))
    segments.append(("00_backchannel", backchannel.text, audio))
    print(f"  played: {_rms_db(audio):.1f}dB")

    for i, text in enumerate(RESPONSE_TURNS, start=1):
        print(f"\nTurn {i}: {text!r}")
        offset = voice_tts.VOICE_LOG_PATH.stat().st_size if voice_tts.VOICE_LOG_PATH.exists() else 0
        audio = await _capture(voice_tts.speak(text))
        gains = _read_new_ramp_gains(offset)
        segments.append((f"{i:02d}_response_turn{i}", text, audio))
        print(f"  played: {_rms_db(audio):.1f}dB, applied ramp_gain_db per chunk: {gains}")

    manifest = []
    concat_parts = []
    silence = np.zeros(int(sample_rate * 0.5), dtype=np.float32)
    for name, text, audio in segments:
        out_path = OUT_DIR / f"{name}.wav"
        sf.write(out_path, audio, sample_rate)
        manifest.append(f"{out_path.name}: {text!r} ({_rms_db(audio):.1f}dB)")
        concat_parts.append(audio)
        concat_parts.append(silence)

    sf.write(OUT_DIR / "sequence_full.wav", np.concatenate(concat_parts), sample_rate)
    (OUT_DIR / "manifest.txt").write_text("\n".join(manifest), encoding="utf-8")

    print(f"\n=== Summary (played level, dB RMS) ===")
    for name, text, audio in segments:
        print(f"  {name}: {_rms_db(audio):5.1f}dB  {text!r}")
    print(f"\nSaved to {OUT_DIR} (sequence_full.wav is the whole thing back to back)")


if __name__ == "__main__":
    asyncio.run(main())
