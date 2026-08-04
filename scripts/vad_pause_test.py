"""Tests [audio.vad].min_silence_duration_ms against real conversational hesitation
pauses - deliberate mid-sentence gaps (before a number, reaching for a word, trailing
off and continuing) - not the clean scripted silences a synthetic test would produce.

Records ONE live capture, then replays it offline through the real EndpointDetector
at each candidate threshold - not three separate live captures - so every setting is
judged against identical input. Every start/end decision is logged alongside the full
transcript of the segment it produced, so a hesitation misread as end-of-utterance
shows up as a transcript that stops mid-thought followed by a new one starting where
it left off. The capture WAV is saved so more thresholds can be tried later without
recording again (--replay).

This script does not decide whether a threshold clips speech - it has no way to know
what you meant to say. It gives you the segmented transcripts to read.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import sounddevice as sd
import tomllib
from scipy.io import wavfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.ears.stt import Transcriber
from services.ears.vad import EndpointDetector

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"
OUT_DIR = ROOT / "logs" / "vad_pause_test"


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)["audio"]


async def record(duration_s: float, sample_rate: int, frame_size: int) -> np.ndarray:
    loop = asyncio.get_running_loop()
    frame_queue: asyncio.Queue = asyncio.Queue()

    def _on_audio(indata, frames, time_info, status) -> None:
        loop.call_soon_threadsafe(frame_queue.put_nowait, indata[:, 0].copy())

    stream = sd.InputStream(
        samplerate=sample_rate, channels=1, dtype="float32", blocksize=frame_size, callback=_on_audio,
    )

    with stream:
        print("Opening mic, waiting for first frame...")
        await asyncio.wait_for(frame_queue.get(), timeout=5.0)
        print("Mic live.")

        for n in (3, 2, 1):
            print(f"  recording starts in {n}...")
            countdown_end = time.perf_counter() + 1.0
            while time.perf_counter() < countdown_end:
                try:
                    await asyncio.wait_for(frame_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass

        print(
            f"RECORDING NOW for {duration_s:.0f}s. Talk naturally, with deliberate mid-sentence "
            f"hesitations - pause before a number, reach for a word, trail off and continue. "
            f"Include a few separate sentences."
        )
        frames = []
        start = time.perf_counter()
        while time.perf_counter() - start < duration_s:
            try:
                frame = await asyncio.wait_for(frame_queue.get(), timeout=1.0)
                frames.append(frame)
            except asyncio.TimeoutError:
                continue
    print("Recording done.\n")
    return np.concatenate(frames)


def run_pass(
    audio: np.ndarray, sample_rate: int, frame_size: int, threshold: float,
    min_silence_duration_ms: int, speech_pad_ms: int, stt: Transcriber,
) -> list[dict]:
    vad = EndpointDetector(
        threshold=threshold, sample_rate=sample_rate,
        min_silence_duration_ms=min_silence_duration_ms, speech_pad_ms=speech_pad_ms,
    )
    utterances = []
    utterance_frames: list[np.ndarray] = []
    in_speech = False
    utterance_start_s = 0.0

    n_frames = len(audio) // frame_size
    for i in range(n_frames):
        frame = audio[i * frame_size:(i + 1) * frame_size]
        event = vad.process_frame(frame)
        t_s = i * frame_size / sample_rate

        if event is not None and event.kind == "start":
            in_speech = True
            utterance_start_s = t_s
            utterance_frames = []

        if in_speech:
            utterance_frames.append(frame)

        if event is not None and event.kind == "end":
            seg_audio = np.concatenate(utterance_frames)
            transcript = stt.transcribe(seg_audio)
            utterances.append({
                "start_s": round(utterance_start_s, 2), "end_s": round(t_s, 2),
                "duration_s": round(t_s - utterance_start_s, 2),
                "vad_latency_ms": round(event.latency_ms, 1),
                "text": transcript.text,
            })
            in_speech = False
            utterance_frames = []

    if in_speech and utterance_frames:
        end_s = n_frames * frame_size / sample_rate
        seg_audio = np.concatenate(utterance_frames)
        transcript = stt.transcribe(seg_audio)
        utterances.append({
            "start_s": round(utterance_start_s, 2), "end_s": round(end_s, 2),
            "duration_s": round(end_s - utterance_start_s, 2),
            "vad_latency_ms": None, "text": transcript.text,
            "note": "recording ended before VAD confirmed end - not a real endpoint decision",
        })

    return utterances


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--replay", type=str, default=None, help="Replay an existing WAV instead of recording")
    parser.add_argument("--thresholds-ms", type=str, default="300,400,500")
    args = parser.parse_args()

    config = _load_config()
    sample_rate = config["sample_rate"]
    frame_size = config["frame_size"]
    vad_cfg = config["vad"]

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.replay:
        replay_path = Path(args.replay)
        sr, audio_i16 = wavfile.read(replay_path)
        assert sr == sample_rate, f"expected {sample_rate}Hz, got {sr}Hz"
        audio = audio_i16.astype(np.float32) / 32768.0
        print(f"Replaying {replay_path}\n")
    else:
        audio = await record(args.duration, sample_rate, frame_size)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        capture_path = OUT_DIR / f"capture_{ts}.wav"
        wavfile.write(capture_path, sample_rate, np.clip(audio * 32768, -32768, 32767).astype(np.int16))
        print(f"Saved capture to {capture_path}")
        print(f"Reuse it later with: uv run scripts/vad_pause_test.py --replay {capture_path}\n")

    stt = Transcriber(
        model_name=config["stt"]["model"], device=config["stt"]["device"],
        compute_type=config["stt"]["compute_type"], language=config["stt"]["language"],
    )

    thresholds = [int(x) for x in args.thresholds_ms.split(",")]
    results = {}
    for ms in thresholds:
        print(f"=== min_silence_duration_ms = {ms} ===")
        utterances = run_pass(
            audio, sample_rate, frame_size, vad_cfg["threshold"], ms, vad_cfg["speech_pad_ms"], stt,
        )
        results[ms] = utterances
        for u in utterances:
            flag = f"  [{u['note']}]" if "note" in u else ""
            print(f"  [{u['start_s']:6.2f}s-{u['end_s']:6.2f}s] ({u['duration_s']:.2f}s) '{u['text']}'{flag}")
        print()

    log_path = OUT_DIR / f"results_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
    with log_path.open("w") as f:
        json.dump(results, f, indent=2)
    print(f"Full results written to {log_path}")

    print("\n=== Utterance count per threshold ===")
    print("Not a verdict by itself - a hesitation split into two utterances raises the count at a")
    print("lower threshold, but so does a natural pause *between* separate sentences. Read the")
    print("transcripts above to tell which happened.")
    for ms in thresholds:
        print(f"  {ms}ms: {len(results[ms])} utterance(s)")


if __name__ == "__main__":
    asyncio.run(main())
