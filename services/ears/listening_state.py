"""Cross-process "is the assistant actively engaged with the user right now"
signal - same pattern as services/voice/playback_state.py (PROMPTS.md A11),
just for the ears side instead of voice playback. services/ears/pipeline.py
writes it on every state transition; ui/'s character window reads it (same
fs.watch-plus-backstop pattern already used for playback_state.json) to
suppress idle wandering while she's mid-conversation.

Deliberately dependency-free, same reasoning as playback_state.py: any
process that wants to check this shouldn't have to import the wake/VAD/STT
stack just to read one flag.
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
STATE_PATH = ROOT / "logs" / "listening_state.json"

_state = {"active": False, "changed_at": None}


def _write() -> None:
    # Same atomic-write-with-tolerant-retry as playback_state.py's _write() -
    # ui/'s character window polls this file frequently enough that a
    # Windows os.replace() collision is a real, not hypothetical, risk here
    # too. Dropping one update is fine (the next transition writes again);
    # raising into pipeline.py's hot frame loop is not.
    STATE_PATH.parent.mkdir(exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(_state))
    for attempt in range(3):
        try:
            tmp.replace(STATE_PATH)
            return
        except OSError as exc:
            if attempt == 2:
                print(f"listening_state: write failed after retries, dropping this update: {exc}", file=sys.stderr)
                tmp.unlink(missing_ok=True)
                return
            time.sleep(0.005 * (attempt + 1))


def mark_active() -> None:
    """Call on entering 'recording' or 'awaiting_resume' - anywhere the
    pipeline is actively engaged with an utterance, not just idly waiting
    for the wake word."""
    _state["active"] = True
    _state["changed_at"] = time.time()
    _write()


def mark_idle() -> None:
    """Call on returning to 'listening' (waiting for the wake word)."""
    _state["active"] = False
    _state["changed_at"] = time.time()
    _write()


def is_active() -> bool:
    try:
        data = json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return False  # no signal ever written, or a torn read - default to "not engaged", not "block forever"
    return bool(data.get("active"))
