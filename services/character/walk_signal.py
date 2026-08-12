"""Python -> Electron signal for the computer-use performance layer's walk
phase (PROMPTS.md A18) - a request/status file pair tools/computer.py uses to
tell the character window "walk toward this X" and wait for it to actually
arrive before the eased cursor move starts, then reset her to idle once the
click sequence finishes. Same atomic-write-with-retry pattern as
services/voice/playback_state.py and services/ears/listening_state.py, and
the same reason for choosing this mechanism over anything else: ui/src/main.ts's
own header comment states a deliberate, standing decision against a new
HTTP/WebSocket/IPC transport between Python and Electron - every existing
Python->Electron signal in this codebase is a small atomically-written JSON
file under logs/, watched via fs.watch() plus a backstop poll
(ui/src/character_main.ts's startCharacterFeatures()). This follows that
precedent rather than deviating from it.

Single-writer-per-file, split both ways: this module owns
computer_walk_request.json (Python writes, Electron watches);
ui/src/character_main.ts owns computer_walk_status.json (Electron writes,
this module watches/polls). Never the reverse for either file - same
discipline playback_state.py's own docstring gives for why it's safe for
multiple readers to poll a file only one process ever writes.
"""

import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
REQUEST_PATH = ROOT / "logs" / "computer_walk_request.json"
STATUS_PATH = ROOT / "logs" / "computer_walk_status.json"

_ARRIVAL_POLL_S = 0.05


def _atomic_write(path: Path, data: dict) -> None:
    # Same tmp+rename-with-retry as playback_state.py's _write() - the same
    # real Windows os.replace()-requires-exclusive-access race that module's
    # docstring documents applies here too, just with the two processes'
    # reader/writer roles reversed per file.
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    for attempt in range(3):
        try:
            tmp.replace(path)
            return
        except OSError as exc:
            if attempt == 2:
                print(f"walk_signal: write failed after retries, dropping this update: {exc}", file=sys.stderr)
                tmp.unlink(missing_ok=True)
                return
            time.sleep(0.005 * (attempt + 1))


def request_walk(target_x: int) -> str:
    """Writes a fresh walk request and returns its request_id - callers pass
    that id to wait_for_arrival() so a stale status record (left over from a
    previous request, or a previous process's crash) can never be mistaken
    for this one having arrived."""
    request_id = uuid.uuid4().hex[:8]
    _atomic_write(REQUEST_PATH, {"request_id": request_id, "action": "walk", "target_x": target_x, "requested_at": time.time()})
    return request_id


def signal_idle() -> str:
    """Resets the character to idle at the end of a computer-use action
    sequence - no walk-back animation, same "honest, not approximated"
    reasoning as skipping a fake reach gesture: she just resets state/
    expression in place, matching what the placeholder rig can actually do."""
    request_id = uuid.uuid4().hex[:8]
    _atomic_write(REQUEST_PATH, {"request_id": request_id, "action": "idle", "requested_at": time.time()})
    return request_id


def wait_for_arrival(request_id: str, timeout_s: float = 5.0) -> bool:
    """Polls computer_walk_status.json (Electron-written) until it shows this
    exact request_id has arrived, or timeout_s elapses. A plain poll, not
    fs.watch-equivalent - this is a short, bounded wait inside one tool call,
    not a long-lived background listener, so the simpler mechanism is enough
    here (unlike the Electron side, which does use fs.watch since it's
    watching continuously across the whole window's lifetime, not one call)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            status = json.loads(STATUS_PATH.read_text())
            if status.get("request_id") == request_id and status.get("state") == "arrived":
                return True
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(_ARRIVAL_POLL_S)
    return False
