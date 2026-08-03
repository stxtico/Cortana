"""Wires wake -> VAD -> STT into one utterance stream. Owns the mic input.

State machine: LISTENING (feed wake, one frame at a time, keep a rolling lookback
buffer) -> on detection -> VERIFYING (if enabled: grab lookback + a short lookahead,
run STT, require the wake phrase actually appears) -> RECORDING (feed VAD, accumulate
audio until an endpoint) -> transcribe -> yield -> back to LISTENING.

Verification exists because wake-word confidence score doesn't separate real
detections from false accepts - calibration runs against background speech showed a
false accept score above most genuine hits. Toggle via [audio.wake].verify.
"""

import asyncio
import json
import time
import tomllib
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import sounddevice as sd

from services.ears.stt import Transcriber
from services.ears.vad import EndpointDetector
from services.ears.wake import WakeWordDetector

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"
EARS_LOG_PATH = ROOT / "logs" / "ears.jsonl"


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)["audio"]


def _log(record: dict) -> None:
    EARS_LOG_PATH.parent.mkdir(exist_ok=True)
    with EARS_LOG_PATH.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}) + "\n")


def _to_int16(frame: np.ndarray) -> np.ndarray:
    return np.clip(frame * 32768.0, -32768, 32767).astype(np.int16)


async def listen() -> AsyncIterator[str]:
    """Yields one transcribed utterance each time the wake word fires, passes
    verification (if enabled), and the following speech reaches an endpoint. Runs
    until the caller stops iterating."""
    config = _load_config()
    sample_rate = config["sample_rate"]
    frame_size = config["frame_size"]
    frame_duration_ms = frame_size / sample_rate * 1000

    wake_cfg = config["wake"]
    wake = WakeWordDetector(model_name=wake_cfg["model"], threshold=wake_cfg["threshold"])
    debounce_s = wake_cfg["debounce_s"]
    verify_enabled = wake_cfg["verify"]
    verify_phrase = wake_cfg["verify_phrase"].lower()
    lookback_frames_n = max(1, round(wake_cfg["verify_lookback_ms"] / frame_duration_ms))
    lookahead_frames_n = max(1, round(wake_cfg["verify_lookahead_ms"] / frame_duration_ms))

    vad = EndpointDetector(
        threshold=config["vad"]["threshold"],
        sample_rate=sample_rate,
        min_silence_duration_ms=config["vad"]["min_silence_duration_ms"],
        speech_pad_ms=config["vad"]["speech_pad_ms"],
    )
    max_utterance_s = config["vad"]["max_utterance_s"]
    stt = Transcriber(
        model_name=config["stt"]["model"],
        device=config["stt"]["device"],
        compute_type=config["stt"]["compute_type"],
        language=config["stt"]["language"],
    )

    loop = asyncio.get_running_loop()
    frame_queue: asyncio.Queue = asyncio.Queue()

    def _on_audio(indata, frames, time_info, status) -> None:
        loop.call_soon_threadsafe(frame_queue.put_nowait, indata[:, 0].copy())

    stream = sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=frame_size,
        callback=_on_audio,
    )

    utterance_id = 0
    state = "listening"
    utterance_frames: list[np.ndarray] = []
    recording_start = 0.0
    last_wake_time = -debounce_s
    lookback: deque[np.ndarray] = deque(maxlen=lookback_frames_n)

    with stream:
        while True:
            frame = await frame_queue.get()

            if state == "listening":
                lookback.append(frame)
                event = wake.process_frame(_to_int16(frame))
                now = time.perf_counter()
                if event is not None and (now - last_wake_time) > debounce_s:
                    last_wake_time = now
                    utterance_id += 1
                    _log({"stage": "wake", "utterance_id": utterance_id, "latency_ms": event.latency_ms, "score": event.score})

                    if not verify_enabled:
                        state = "recording"
                        utterance_frames = []
                        vad.reset()
                        recording_start = now
                        continue

                    verify_start = time.perf_counter()
                    lookahead = [await frame_queue.get() for _ in range(lookahead_frames_n)]
                    verify_audio = np.concatenate(list(lookback) + lookahead)
                    verify_transcript = stt.transcribe(verify_audio)
                    verify_latency_ms = (time.perf_counter() - verify_start) * 1000
                    passed = verify_phrase in verify_transcript.text.lower()
                    _log({
                        "stage": "verify", "utterance_id": utterance_id, "latency_ms": verify_latency_ms,
                        "text": verify_transcript.text, "passed": passed,
                    })

                    if passed:
                        state = "recording"
                        utterance_frames = []
                        vad.reset()
                        recording_start = time.perf_counter()
                    # else: discard, stay in listening, lookback keeps rolling

            elif state == "recording":
                utterance_frames.append(frame)
                vad_event = vad.process_frame(frame)
                timed_out = (time.perf_counter() - recording_start) > max_utterance_s
                if (vad_event is not None and vad_event.kind == "end") or timed_out:
                    latency_ms = vad_event.latency_ms if vad_event else max_utterance_s * 1000
                    _log({"stage": "vad", "utterance_id": utterance_id, "latency_ms": latency_ms, "timed_out": timed_out})

                    audio = np.concatenate(utterance_frames)
                    transcript = stt.transcribe(audio)
                    _log({"stage": "stt", "utterance_id": utterance_id, "latency_ms": transcript.latency_ms, "text": transcript.text})

                    if transcript.text:
                        yield transcript.text

                    state = "listening"
                    utterance_frames = []
                    lookback.clear()


async def _main() -> None:
    print("Listening for the wake word... (Ctrl+C to stop)\n")
    async for text in listen():
        print(f"> {text}")


if __name__ == "__main__":
    asyncio.run(_main())
