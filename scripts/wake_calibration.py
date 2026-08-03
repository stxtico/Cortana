"""Live-mic calibration tool: logs every frame's wake-word score (not just threshold
crossings) plus full VAD/STT latency for real detections. Not part of the production
pipeline - services/ears/pipeline.py only logs actual detections, this logs everything
so a confidence-score distribution can be built before tuning any threshold.

Mirrors pipeline.py's verification gate (same [audio.wake] config) so false-accept
reduction can be measured with real mic data, not just assumed.
"""

import argparse
import asyncio
import json
import os
import sys
import time
import tomllib
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import sounddevice as sd
from openwakeword.model import Model

from services.ears.stt import Transcriber
from services.ears.vad import EndpointDetector
from services.ears.wake import _resolve_model_path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"
OUT_PATH = ROOT / "logs" / "wake_calibration.jsonl"


def _to_int16(frame: np.ndarray) -> np.ndarray:
    return np.clip(frame * 32768.0, -32768, 32767).astype(np.int16)


def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


async def main(duration_s: float, status_interval_s: float = 5.0) -> None:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)["audio"]

    sample_rate = config["sample_rate"]
    frame_size = config["frame_size"]
    frame_duration_ms = frame_size / sample_rate * 1000
    wake_cfg = config["wake"]
    threshold = wake_cfg["threshold"]
    debounce_s = wake_cfg["debounce_s"]
    verify_enabled = wake_cfg["verify"]
    verify_phrase = wake_cfg["verify_phrase"].lower()
    lookback_frames_n = max(1, round(wake_cfg["verify_lookback_ms"] / frame_duration_ms))
    lookahead_frames_n = max(1, round(wake_cfg["verify_lookahead_ms"] / frame_duration_ms))

    # Raw model access (not WakeWordDetector) so every frame's score gets logged,
    # not just ones that cross the threshold.
    model_path = _resolve_model_path(wake_cfg["model"])
    raw_model = Model(wakeword_model_paths=[model_path])
    key = os.path.basename(model_path)[: -len(".onnx")]

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
        samplerate=sample_rate, channels=1, dtype="float32", blocksize=frame_size, callback=_on_audio,
    )

    log_f = OUT_PATH.open("w")

    def log(record: dict) -> None:
        log_f.write(json.dumps({"t": time.perf_counter(), "ts": datetime.now(timezone.utc).isoformat(), **record}) + "\n")
        log_f.flush()

    state = "listening"
    utterance_frames: list[np.ndarray] = []
    recording_start = 0.0
    last_wake_time = -debounce_s
    utterance_id = 0
    lookback: deque = deque(maxlen=lookback_frames_n)

    # Live density check - the original 23-detection runs averaged ~1.3-2.0 frames/sec
    # scoring above 0.9. Printed periodically so a too-quiet background can be caught
    # and fixed instead of burning the full capture window on a non-representative test.
    high_score_count = 0
    listening_frame_count = 0
    last_status_time = 0.0

    with stream:
        _safe_print("Opening mic stream, waiting for first frame...")
        try:
            first_frame = await asyncio.wait_for(frame_queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            raise RuntimeError(
                "No audio frames arrived within 5s of opening the mic stream - the "
                "input device isn't delivering audio. Check the selected device / OS mic "
                "permissions before trusting any capture from this run."
            )
        _safe_print(f"Mic confirmed live (first frame received). Listening for {duration_s:.0f}s now "
                    f"(verify={'on' if verify_enabled else 'off'}). Say \"hey jarvis\" several times "
                    f"(that's the current stand-in model), mix in normal conversation and background "
                    f"noise. Ctrl+C to stop early.\n")

        start_time = time.perf_counter()
        pending_frames = [first_frame]

        while time.perf_counter() - start_time < duration_s:
            if pending_frames:
                frame = pending_frames.pop(0)
            else:
                try:
                    frame = await asyncio.wait_for(frame_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

            if state == "listening":
                lookback.append(frame)
                infer_start = time.perf_counter()
                score = float(raw_model.predict(_to_int16(frame))[key])
                latency_ms = (time.perf_counter() - infer_start) * 1000
                log({"stage": "wake_score", "score": score, "latency_ms": latency_ms})

                listening_frame_count += 1
                if score >= 0.9:
                    high_score_count += 1

                elapsed = time.perf_counter() - start_time
                if elapsed - last_status_time >= status_interval_s:
                    last_status_time = elapsed
                    listening_s = listening_frame_count * frame_duration_ms / 1000
                    rate = high_score_count / listening_s if listening_s > 0 else 0.0
                    _safe_print(f"  [status t={elapsed:.0f}s] frames>0.9: {high_score_count} over {listening_s:.1f}s "
                                f"listening = {rate:.2f}/s (original runs: ~1.3-2.0/s)")

                now = time.perf_counter()
                if score >= threshold and (now - last_wake_time) > debounce_s:
                    last_wake_time = now
                    utterance_id += 1
                    log({"stage": "wake_detect", "utterance_id": utterance_id, "score": score, "latency_ms": latency_ms})
                    _safe_print(f"[{utterance_id}] WAKE detected, score={score:.3f}")

                    if not verify_enabled:
                        state = "recording"
                        utterance_frames = []
                        vad.reset()
                        recording_start = now
                        continue

                    verify_start = time.perf_counter()
                    lookahead = []
                    for _ in range(lookahead_frames_n):
                        lookahead.append(await asyncio.wait_for(frame_queue.get(), timeout=1.0))
                    verify_audio = np.concatenate(list(lookback) + lookahead)
                    verify_transcript = stt.transcribe(verify_audio)
                    verify_latency_ms = (time.perf_counter() - verify_start) * 1000
                    passed = verify_phrase in verify_transcript.text.lower()
                    log({
                        "stage": "verify", "utterance_id": utterance_id, "latency_ms": verify_latency_ms,
                        "text": verify_transcript.text, "passed": passed,
                    })
                    _safe_print(f"[{utterance_id}] verify: '{verify_transcript.text}' -> {'PASS' if passed else 'REJECT'} ({verify_latency_ms:.0f}ms)")

                    if passed:
                        state = "recording"
                        utterance_frames = []
                        vad.reset()
                        recording_start = time.perf_counter()

            elif state == "recording":
                utterance_frames.append(frame)
                vad_event = vad.process_frame(frame)
                timed_out = (time.perf_counter() - recording_start) > max_utterance_s
                if (vad_event is not None and vad_event.kind == "end") or timed_out:
                    vad_latency_ms = vad_event.latency_ms if vad_event else max_utterance_s * 1000
                    log({"stage": "vad_end", "utterance_id": utterance_id, "latency_ms": vad_latency_ms, "timed_out": timed_out})

                    audio = np.concatenate(utterance_frames)
                    transcript = stt.transcribe(audio)
                    log({"stage": "stt", "utterance_id": utterance_id, "latency_ms": transcript.latency_ms, "text": transcript.text})
                    _safe_print(f"[{utterance_id}] -> '{transcript.text}' (vad={vad_latency_ms:.0f}ms, stt={transcript.latency_ms:.0f}ms)")

                    state = "listening"
                    utterance_frames = []
                    lookback.clear()

    log_f.close()
    _safe_print(f"\nDone. Log written to {OUT_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=180.0)
    parser.add_argument("--probe", action="store_true",
                         help="Quick 20s check with frequent density status prints - confirm "
                              "background audio is actually reaching the mic before committing "
                              "to a full capture. Overrides --duration.")
    args = parser.parse_args()

    if args.probe:
        asyncio.run(main(duration_s=20.0, status_interval_s=2.0))
    else:
        asyncio.run(main(args.duration))
