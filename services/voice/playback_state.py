"""Cross-process "is a response currently playing" signal (PROMPTS.md A11) -
a tiny atomically-written file, not an IPC framework. services/voice/tts.py
writes it whenever real playback starts/stops (alongside its own in-process
_response_playback_active flag, which services/brain/loop.py's barge-in logic
already used - this is the same signal, just readable from outside this
process). services/daemon/daemon.py, a genuinely separate process, reads it
before ever announcing anything, so proactive output never talks over a
response already in progress.

Deliberately dependency-free - no torch, no XTTS, nothing beyond the standard
library - so the daemon, a lightweight background watcher, never needs to
import the whole voice stack (and its GPU/model loading) just to check one
flag. Import this module on its own, never `services.voice.tts` from
services/daemon/.
"""

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
STATE_PATH = ROOT / "logs" / "playback_state.json"


def _write(data: dict) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(STATE_PATH)  # atomic rename on the same filesystem - no reader ever sees a half-written file


def mark_started() -> None:
    _write({"active": True, "started_at": time.time()})


def mark_stopped() -> None:
    _write({"active": False, "started_at": None})


def is_active() -> bool:
    try:
        data = json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return False  # no signal ever written, or a torn read - default to "not playing", not "block forever"
    return bool(data.get("active"))
