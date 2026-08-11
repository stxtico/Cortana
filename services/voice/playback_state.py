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
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
STATE_PATH = ROOT / "logs" / "playback_state.json"

# In-memory mirror of the last-written state - this module is single-writer
# (only services/voice/tts.py's process ever calls the mark_*/update_*
# functions below), so caching it here is safe and lets each write function
# update just its own field without re-reading the file first.
_state = {"active": False, "started_at": None, "amplitude": 0.0}


def _write() -> None:
    """Atomic rename, with a tolerant retry for a real Windows-specific race
    (PROMPTS.md A15): found live, not hypothetical - ui/'s character window
    reads this file frequently (fs.watch plus a backstop poll, for lip sync),
    and Windows' os.replace() requires exclusive access to the destination,
    unlike POSIX rename - a reader's brief open-read-close can collide with
    the rename and raise PermissionError ("Access is denied"). This call
    site (services/voice/tts.py's _write_interruptible(), the hot loop that
    calls update_amplitude() ~10x/sec during real playback) does NOT catch
    exceptions - an unhandled one here would abort real audio playback
    mid-sentence, a far worse outcome than dropping one state update. Amplitude
    in particular is inherently best-effort (PLAN.md: "cheap and convincing,"
    not phoneme-accurate) - missing one write out of ten a second is invisible;
    crashing playback is not acceptable for any of this module's callers, so
    the tolerance lives here once, not duplicated at every call site."""
    STATE_PATH.parent.mkdir(exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(_state))
    for attempt in range(3):
        try:
            tmp.replace(STATE_PATH)  # atomic rename on the same filesystem - no reader ever sees a half-written file
            return
        except OSError as exc:
            if attempt == 2:
                print(f"playback_state: write failed after retries, dropping this update: {exc}", file=sys.stderr)
                tmp.unlink(missing_ok=True)
                return
            time.sleep(0.005 * (attempt + 1))


def mark_started() -> None:
    _state["active"] = True
    _state["started_at"] = time.time()
    _write()


def mark_stopped() -> None:
    _state["active"] = False
    _state["started_at"] = None
    _state["amplitude"] = 0.0  # mouth should close, not hold whatever level it last had
    _write()


def update_amplitude(rms: float) -> None:
    """Live audio level during playback (PROMPTS.md A15 - lip sync driven by
    TTS audio amplitude). Written from services/voice/tts.py's
    _write_interruptible(), the one shared playback primitive both
    play_audio() (backchannels) and _play_all() (real responses) already
    go through, so both naturally drive the mouth - not two separate hookup
    points. Updated roughly once per ~100ms sub-block (_PLAYBACK_SUBBLOCK_S),
    not per-sample - cheap and convincing, matching PLAN.md's own framing of
    amplitude-driven lip sync, not a claim of phoneme-accurate mouth shapes."""
    _state["amplitude"] = rms
    _write()


def is_active() -> bool:
    try:
        data = json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return False  # no signal ever written, or a torn read - default to "not playing", not "block forever"
    return bool(data.get("active"))
