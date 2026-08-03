"""Wires wake -> VAD -> STT into one utterance stream. Owns the mic input.

State machine: LISTENING (feed wake, one frame at a time) -> on detection ->
RECORDING (feed VAD, accumulate audio until an endpoint) -> transcribe -> yield ->
back to LISTENING.
"""

import asyncio
import json
import time
import tomllib
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
    """Yields one transcribed utterance each time the wake word fires and the
    following speech reaches an endpoint. Runs until the caller stops iterating."""
    config = _load_config()
    sample_rate = config["sample_rate"]
    frame_size = config["frame_size"]

    wake = WakeWordDetector(model_name=config["wake"]["model"], threshold=config["wake"]["threshold"])
    debounce_s = config["wake"]["debounce_s"]
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

    with stream:
        while True:
            frame = await frame_queue.get()

            if state == "listening":
                event = wake.process_frame(_to_int16(frame))
                now = time.perf_counter()
                if event is not None and (now - last_wake_time) > debounce_s:
                    last_wake_time = now
                    utterance_id += 1
                    _log({"stage": "wake", "utterance_id": utterance_id, "latency_ms": event.latency_ms, "score": event.score})
                    state = "recording"
                    utterance_frames = []
                    vad.reset()
                    recording_start = now

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


async def _main() -> None:
    print("Listening for the wake word... (Ctrl+C to stop)\n")
    async for text in listen():
        print(f"> {text}")


if __name__ == "__main__":
    asyncio.run(_main())
