"""Cross-process "here's the greeting to speak" handoff - same tiny
atomically-written-file shape as services/voice/playback_state.py and
services/memory/session_state.py, not an IPC framework. Owned by the
writer (services/daemon/session_trigger.py, composes the text) since that's
the established convention for these signal files; read and consumed
(deleted after reading, so it's only ever spoken once) by
services/brain/loop.py, the only process that can actually call
services/voice/tts.py's speak().

Stored under daemon_store/ (not logs/) - it's daemon-produced application
state the same way daemon_store/timers.json is, not a log record.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SIGNAL_PATH = ROOT / "daemon_store" / "pending_greeting.json"


def write_greeting(text: str, session_id: str) -> None:
    SIGNAL_PATH.parent.mkdir(exist_ok=True)
    tmp = SIGNAL_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"text": text, "session_id": session_id}))
    tmp.replace(SIGNAL_PATH)


def read_and_consume(session_id: str) -> str | None:
    """Only returns the greeting if it was composed for THIS session_id -
    guards against a stale leftover file (e.g. the daemon composed one for
    a session that crashed before loop.py ever read it) being spoken at the
    start of some later, unrelated session. Deletes the file either way once
    read, so a stale one doesn't linger forever."""
    try:
        data = json.loads(SIGNAL_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    SIGNAL_PATH.unlink(missing_ok=True)
    if data.get("session_id") != session_id:
        return None
    return data.get("text")
