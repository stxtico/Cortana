"""Cross-process "a new conversation session just started" signal - same
shape as services/voice/playback_state.py (a tiny atomically-written file,
not an IPC framework), same reasoning: services/daemon/session_trigger.py,
a genuinely separate process, needs to know a new services/brain/loop.py
session began without importing the memory/sqlite-vec stack itself.

Deliberately dependency-free (stdlib only) for the same reason
playback_state.py is - the daemon is a lightweight background watcher and
should never need to pull in a heavier dependency just to check one signal.

Single-writer (services/memory/manager.py's MemoryManager.__post_init__,
the same place session_id is already generated - PROMPTS.md A6/A11
follow-up), single-reader-across-process (session_trigger.py). No retry-
tolerant write like playback_state.py's _write() has - this is written
once per process lifetime, not ~10x/sec, so the contention that write()'s
retry loop exists for doesn't apply here; a plain atomic rename is enough.
"""

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
STATE_PATH = ROOT / "logs" / "session_state.json"


def write_session_start(session_id: str) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"session_id": session_id, "started_at": time.time()}))
    tmp.replace(STATE_PATH)  # atomic rename - no reader ever sees a half-written file


def read() -> dict | None:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
