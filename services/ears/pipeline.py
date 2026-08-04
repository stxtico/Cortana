"""Wires wake -> VAD -> STT into one utterance stream. Owns the mic input.

State machine: LISTENING (feed wake, one frame at a time, keep a rolling lookback
buffer) -> on detection -> RECORDING (feed VAD immediately; verification runs
concurrently in a background thread over the lookback + first lookahead frames of
the same recording, not sequentially before it) -> transcribe -> completeness check
-> either yield (finished thought) or AWAITING_RESUME (abandoned - play a
backchannel and listen for a resume without requiring the wake word again, since a
person continuing a thought doesn't re-say the assistant's name) -> back to
LISTENING. If verification rejects, the recording is discarded, whether that's
noticed mid-recording (checked every frame) or only at finalize time.

Verification exists because wake-word confidence score doesn't separate real
detections from false accepts - calibration runs against background speech showed a
false accept score above most genuine hits. Toggle via [audio.wake].verify.

Verification used to run before RECORDING started, costing ~200-1050ms of pure
added latency on every trigger including genuine ones. Running it concurrently
with RECORDING hides that cost on true positives - verification is usually resolved
before VAD would naturally end the utterance anyway, so the fast path pays nothing
extra. Only very short genuine utterances (VAD ends before verification resolves)
pay a residual wait, bounded by whatever verification time hadn't yet elapsed.

AWAITING_RESUME exists because min_silence_duration_ms was raised from 300ms to
600ms after scripts/vad_pause_test.py showed real hesitation gaps run 582-1822ms -
no threshold in a usable range avoids clipping a genuine mid-thought pause. Instead
of guessing wrong silently, services/ears/completeness.py flags a likely-abandoned
transcript, services/ears/backchannel.py decides whether to play a pre-rendered
backchannel (never generated live - see services/ears/backchannel_pool.py) and how
long to wait, and a resumed utterance gets appended to the pending fragment rather
than starting fresh. Toggle via [audio.backchannel].enabled.
"""

import asyncio
import json
import time
import tomllib
from collections import deque
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import sounddevice as sd

from services.ears.backchannel import BackchannelSession
from services.ears.backchannel_pool import get_pool
from services.ears.stt import Transcriber, Transcript
from services.ears.vad import EndpointDetector
from services.ears.wake import WakeWordDetector
from services.voice.tts import play_audio

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


async def listen(on_wake: Callable[[], None] | None = None) -> AsyncIterator[str]:
    """Yields one transcribed utterance each time the wake word fires, passes
    verification (if enabled), and the following speech reaches an endpoint. Runs
    until the caller stops iterating.

    on_wake, if given, fires synchronously the instant a debounced wake event is
    detected - before verification, before recording even starts. This is the
    earliest signal this architecture has for "the user wants attention now," and
    it's what services/brain/loop.py hangs barge-in off: cancelling in-flight TTS
    playback can't wait for verification/VAD-end/STT to finish, or "immediately"
    (PLAN.md's barge-in spec) wouldn't be true. A false wake accept firing this
    early could cut off playback unnecessarily - the trained model's measured
    false-accept rate was low enough in calibration to accept that trade, not
    something this function tries to filter."""
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

    backchannel_cfg = config.get("backchannel", {})
    session = None
    if backchannel_cfg.get("enabled", False):
        session = BackchannelSession(
            resume_window_s=backchannel_cfg.get("resume_window_s", 4.0),
            base_cooldown_s=backchannel_cfg.get("base_cooldown_s", 20.0),
            escalation_factor=backchannel_cfg.get("escalation_factor", 3.0),
            max_cooldown_s=backchannel_cfg.get("max_cooldown_s", 300.0),
        )
        asyncio.ensure_future(get_pool().ensure_filled())  # eager pre-fill, non-blocking

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
    pre_trigger_audio: list[np.ndarray] = []
    verify_task: asyncio.Task | None = None
    verify_task_start = 0.0
    verify_confirmed = False
    resume_deadline = 0.0

    async def _run_verify(audio: np.ndarray) -> Transcript:
        return await asyncio.to_thread(stt.transcribe, audio)

    def _log_verify(text: str, latency_ms: float, passed: bool) -> None:
        _log({
            "stage": "verify", "utterance_id": utterance_id, "latency_ms": latency_ms,
            "text": text, "passed": passed,
        })

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
                    if on_wake is not None:
                        on_wake()

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
                        verify_task = asyncio.ensure_future(_run_verify(verify_audio))

                    if verify_task is not None and verify_task.done():
                        transcript = verify_task.result()
                        latency_ms = (time.perf_counter() - verify_task_start) * 1000
                        passed = verify_phrase in transcript.text.lower()
                        _log_verify(transcript.text, latency_ms, passed)
                        if not passed:
                            state = "listening"
                            utterance_frames = []
                            continue
                        verify_confirmed = True

                vad_event = vad.process_frame(frame)
                timed_out = (time.perf_counter() - recording_start) > max_utterance_s
                if (vad_event is not None and vad_event.kind == "end") or timed_out:
                    if not verify_confirmed:
                        # VAD ended before verification resolved (or even started, for a
                        # very short utterance) - we need an answer before finalizing.
                        if verify_task is None:
                            verify_audio = np.concatenate(pre_trigger_audio + utterance_frames)
                            verify_task_start = time.perf_counter()
                            verify_task = asyncio.ensure_future(_run_verify(verify_audio))
                        transcript = await verify_task
                        latency_ms = (time.perf_counter() - verify_task_start) * 1000
                        passed = verify_phrase in transcript.text.lower()
                        _log_verify(transcript.text, latency_ms, passed)
                        if not passed:
                            state = "listening"
                            utterance_frames = []
                            continue

                    latency_ms = vad_event.latency_ms if vad_event else max_utterance_s * 1000
                    _log({"stage": "vad", "utterance_id": utterance_id, "latency_ms": latency_ms, "timed_out": timed_out})
                    vad_end_time = time.perf_counter()

                    audio = np.concatenate(utterance_frames)
                    transcript = stt.transcribe(audio)
                    _log({"stage": "stt", "utterance_id": utterance_id, "latency_ms": transcript.latency_ms, "text": transcript.text})

                    if transcript.text and session is not None:
                        decision = session.handle_utterance(transcript.text, audio, sample_rate)
                        if decision.action == "yield":
                            state = "listening"
                            lookback.clear()
                            yield decision.text
                        else:
                            if decision.action == "backchannel":
                                # From VAD-end (the user actually stopped talking) to
                                # play_audio() starting - how long someone waits in
                                # silence before hearing the backchannel, not just how
                                # long the pool lookup itself took.
                                backchannel_latency_ms = (time.perf_counter() - vad_end_time) * 1000
                                _log({
                                    "stage": "backchannel", "utterance_id": utterance_id,
                                    "text": decision.backchannel.text,
                                    "latency_ms": round(backchannel_latency_ms, 1),
                                })
                                await play_audio(decision.backchannel.audio, decision.backchannel.sample_rate)
                                asyncio.ensure_future(get_pool().ensure_filled())
                            else:
                                _log({"stage": "backchannel", "utterance_id": utterance_id, "text": None, "latency_ms": None})
                            state = "awaiting_resume"
                            resume_deadline = time.perf_counter() + decision.resume_window_s
                            vad.reset()
                    elif transcript.text:
                        state = "listening"
                        lookback.clear()
                        yield transcript.text
                    else:
                        state = "listening"
                        lookback.clear()

                    utterance_frames = []

            elif state == "awaiting_resume":
                vad_event = vad.process_frame(frame)
                if vad_event is not None and vad_event.kind == "start":
                    state = "recording"
                    utterance_frames = [frame]
                    recording_start = time.perf_counter()
                    pre_trigger_audio = []
                    verify_task = None
                    verify_confirmed = True  # continuing an existing exchange, not a fresh wake trigger
                elif time.perf_counter() > resume_deadline:
                    pending = session.handle_resume_timeout()
                    _log({"stage": "backchannel_timeout", "utterance_id": utterance_id, "yielded": bool(pending)})
                    state = "listening"
                    lookback.clear()
                    if pending:
                        yield pending


async def _main() -> None:
    print("Listening for the wake word... (Ctrl+C to stop)\n")
    async for text in listen():
        print(f"> {text}")


if __name__ == "__main__":
    asyncio.run(_main())
