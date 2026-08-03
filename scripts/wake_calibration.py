"""Live-mic calibration tool: logs every frame's wake-word score (not just threshold
crossings) plus full VAD/STT latency for real detections. Not part of the production
pipeline - services/ears/pipeline.py only logs actual detections, this logs everything
so a confidence-score distribution can be built before tuning any threshold.

Mirrors pipeline.py's concurrent verification gate (same [audio.wake] config, same
recording-starts-immediately-verification-runs-in-parallel design) so results are
directly comparable to production behavior. Rejected verification clips are saved as
WAV files under services/ears/hard_negatives/ - real hard-negative training data for
WAKE_TRAINING.md, not just the transcript text.
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
from scipy.io import wavfile

from services.ears.stt import Transcriber
from services.ears.vad import EndpointDetector
from services.ears.wake import _resolve_model_path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"
OUT_PATH = ROOT / "logs" / "wake_calibration.jsonl"
HARD_NEGATIVES_DIR = ROOT / "services" / "ears" / "hard_negatives"
HARD_NEGATIVES_MANIFEST = HARD_NEGATIVES_DIR / "manifest.jsonl"


def _to_int16(frame: np.ndarray) -> np.ndarray:
    return np.clip(frame * 32768.0, -32768, 32767).astype(np.int16)


def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


def _slug(text: str, max_len: int = 40) -> str:
    keep = "".join(c if c.isalnum() else "_" for c in text)
    return keep.strip("_")[:max_len] or "clip"


def _save_hard_negative(audio: np.ndarray, sample_rate: int, utterance_id: int, score: float, text: str) -> str:
    HARD_NEGATIVES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    filename = f"{ts}_id{utterance_id}_{_slug(text)}.wav"
    path = HARD_NEGATIVES_DIR / filename
    wavfile.write(path, sample_rate, _to_int16(audio))
    with HARD_NEGATIVES_MANIFEST.open("a") as f:
        f.write(json.dumps({
            "file": filename, "utterance_id": utterance_id, "wake_score": score,
            "verify_text": text, "saved_at": datetime.now(timezone.utc).isoformat(),
        }) + "\n")
    return filename


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

    async def run_verify(audio: np.ndarray):
        return await asyncio.to_thread(stt.transcribe, audio)

    state = "listening"
    utterance_frames: list[np.ndarray] = []
    recording_start = 0.0
    last_wake_time = -debounce_s
    utterance_id = 0
    lookback: deque = deque(maxlen=lookback_frames_n)
    pre_trigger_audio: list = []
    verify_task = None
    verify_task_start = 0.0
    verify_confirmed = False
    last_wake_score = 0.0

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
                    f"(verify={'on' if verify_enabled else 'off'}, concurrent). Say \"hey jarvis\" "
                    f"several times (that's the current stand-in model), mix in normal conversation "
                    f"and background noise. Ctrl+C to stop early.\n")

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
                    last_wake_score = score
                    utterance_id += 1
                    log({"stage": "wake_detect", "utterance_id": utterance_id, "score": score, "latency_ms": latency_ms})
                    _safe_print(f"[{utterance_id}] WAKE detected, score={score:.3f}")

                    state = "recording"
                    utterance_frames = []
                    vad.reset()
                    recording_start = now
                    pre_trigger_audio = list(lookback)
                    verify_task = None
                    verify_confirmed = not verify_enabled

            elif state == "recording":
                utterance_frames.append(frame)

                if not verify_confirmed:
                    if verify_task is None and len(utterance_frames) >= lookahead_frames_n:
                        verify_audio = np.concatenate(pre_trigger_audio + utterance_frames[:lookahead_frames_n])
                        verify_task_start = time.perf_counter()
                        verify_task = asyncio.ensure_future(run_verify(verify_audio))

                    if verify_task is not None and verify_task.done():
                        transcript = verify_task.result()
                        verify_latency_ms = (time.perf_counter() - verify_task_start) * 1000
                        passed = verify_phrase in transcript.text.lower()
                        log({
                            "stage": "verify", "utterance_id": utterance_id, "latency_ms": verify_latency_ms,
                            "text": transcript.text, "passed": passed,
                        })
                        _safe_print(f"[{utterance_id}] verify: '{transcript.text}' -> {'PASS' if passed else 'REJECT'} ({verify_latency_ms:.0f}ms)")
                        if not passed:
                            fname = _save_hard_negative(verify_audio, sample_rate, utterance_id, last_wake_score, transcript.text)
                            _safe_print(f"[{utterance_id}] saved hard negative: {fname}")
                            state = "listening"
                            utterance_frames = []
                            continue
                        verify_confirmed = True

                vad_event = vad.process_frame(frame)
                timed_out = (time.perf_counter() - recording_start) > max_utterance_s
                if (vad_event is not None and vad_event.kind == "end") or timed_out:
                    if not verify_confirmed:
                        if verify_task is None:
                            verify_audio = np.concatenate(pre_trigger_audio + utterance_frames)
                            verify_task_start = time.perf_counter()
                            verify_task = asyncio.ensure_future(run_verify(verify_audio))
                        transcript = await verify_task
                        verify_latency_ms = (time.perf_counter() - verify_task_start) * 1000
                        passed = verify_phrase in transcript.text.lower()
                        log({
                            "stage": "verify", "utterance_id": utterance_id, "latency_ms": verify_latency_ms,
                            "text": transcript.text, "passed": passed,
                        })
                        _safe_print(f"[{utterance_id}] verify: '{transcript.text}' -> {'PASS' if passed else 'REJECT'} ({verify_latency_ms:.0f}ms)")
                        if not passed:
                            verify_audio_full = np.concatenate(pre_trigger_audio + utterance_frames)
                            fname = _save_hard_negative(verify_audio_full, sample_rate, utterance_id, last_wake_score, transcript.text)
                            _safe_print(f"[{utterance_id}] saved hard negative: {fname}")
                            state = "listening"
                            utterance_frames = []
                            continue

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
