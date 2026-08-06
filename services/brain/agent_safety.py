"""Dispatcher-enforced safety machinery for services/brain/agent.py (PROMPTS.md
A9): the confirmation gate, the no-credentials rule, and the abort hotkey all
live here as real code that runs regardless of what the model says, not as
persona instructions. Persona-level negative constraints measured at roughly
two-thirds reliability under real testing (A5a's padding investigation,
CLAUDE.md) - nowhere near a safety bar. A gate the model can talk its way past
isn't a gate.

Confirmation is keyboard-only right now, via services/brain/user_input.py's
shared get_answer() - services/ears/pipeline.py's listen() (the mic -> STT
path) isn't wired into agent.py yet, so there is no way for a spoken "yes" to
reach this dispatcher. This is a real, current limitation, not a placeholder
pretending to work: until agent.py is wired into services/brain/loop.py's
conversation loop, "confirm" means typing at this process's stdin. Voice
confirmation is future work for that wiring - see user_input.py's docstring
for why the mechanism is shared with tools/ask_user.py while this function's
gate semantics (yes/no, stops an action already decided, the model can't
route around it) stay this module's own and aren't confused with ask_user's
(free text, the model choosing to ask before deciding anything).
"""

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from services.brain import user_input

ROOT = Path(__file__).resolve().parent.parent.parent
AGENT_LOG_PATH = ROOT / "logs" / "agent.jsonl"

# Heuristic, not exhaustive - a determined adversary could construct something
# that slips past a regex. Real backstop against the actual failure modes this
# guards against: a future tool built with a credential-shaped parameter (this
# catches it even if that tool module forgot to avoid it), or the model
# echoing something secret-looking it picked up from a fetched page (A8's
# fetch_url reads arbitrary pages, which can contain text addressed to the
# model) back into a tool call.
_CREDENTIAL_KEY_RE = re.compile(r"(pass(word)?|secret|api[_-]?key|apikey|token|auth|credential|private[_-]?key)", re.IGNORECASE)
_CREDENTIAL_VALUE_RE = re.compile(r"^(sk-|ghp_|gho_|glpat-|AKIA|Bearer\s)", re.IGNORECASE)


def _log(record: dict) -> None:
    AGENT_LOG_PATH.parent.mkdir(exist_ok=True)
    with AGENT_LOG_PATH.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}) + "\n")


def credential_violation(arguments: dict) -> str | None:
    """Real, code-level enforcement of "no credentials, ever, in any tool"
    (CLAUDE.md rule 4 / A9) - scans tool-call arguments for parameter names
    that look like they're carrying a secret, or values shaped like common
    credential formats, and refuses the call outright if either matches.
    Returns a reason string if it should be refused, None if clean."""
    for key, value in arguments.items():
        if not isinstance(value, str) or not value.strip():
            continue
        if _CREDENTIAL_KEY_RE.search(key):
            return f"argument {key!r} looks like it's carrying a credential - refusing"
        if _CREDENTIAL_VALUE_RE.match(value.strip()):
            return f"argument {key!r}'s value is shaped like a credential - refusing"
    return None


async def confirm(description: str, timeout_s: float = 120.0) -> bool:
    """Blocks until the user answers (via user_input.get_answer(), currently
    CLI) or the timeout elapses - a confirmation that times out is treated as
    declined, not as hung forever."""
    print(f"\n[CONFIRMATION NEEDED]\n{description}")
    try:
        answer = await asyncio.wait_for(user_input.get_answer("Proceed? [y/N] "), timeout=timeout_s)
    except asyncio.TimeoutError:
        print("(no response within the timeout - treating as declined)")
        _log({"stage": "confirmation", "outcome": "timeout"})
        return False
    decided = answer.strip().lower() in ("y", "yes")
    _log({"stage": "confirmation", "outcome": "confirmed" if decided else "declined"})
    return decided


# --- Abort hotkey -----------------------------------------------------------
# Global (works without terminal focus) via the `keyboard` package's low-level
# Windows hook - runs on its own background thread that the package manages
# internally; install_abort_hotkey() only registers a callback and returns.

_current_task: "asyncio.Task | None" = None
_current_loop: "asyncio.AbstractEventLoop | None" = None
_hotkey_installed = False


def register_current_task(task: "asyncio.Task", loop: "asyncio.AbstractEventLoop") -> None:
    global _current_task, _current_loop
    _current_task = task
    _current_loop = loop


def clear_current_task() -> None:
    global _current_task
    _current_task = None


def _on_abort_hotkey() -> None:
    # Runs on the keyboard package's hook thread, not the event loop thread -
    # call_soon_threadsafe is the real cross-thread bridge (same pattern
    # services/ears/pipeline.py uses for sounddevice's mic callback).
    if _current_task is not None and _current_loop is not None and not _current_task.done():
        _current_loop.call_soon_threadsafe(_current_task.cancel)
        print("\n[ABORT] hotkey pressed - cancelling the in-progress tool call.")
        _log({"stage": "abort_hotkey", "outcome": "cancelled"})


def install_abort_hotkey(hotkey: str) -> bool:
    """Returns True if the hook actually registered. Windows low-level
    keyboard hooks don't need admin for a normal-integrity process listening
    to its own session, but this can still fail in some environments (e.g. a
    higher-integrity foreground window) - fails loudly via the return value
    instead of silently doing nothing, so run_agent() can log it rather than
    give a false sense that abort is armed when it isn't."""
    global _hotkey_installed
    if _hotkey_installed:
        return True
    try:
        import keyboard
        keyboard.add_hotkey(hotkey, _on_abort_hotkey)
        _hotkey_installed = True
        return True
    except Exception as exc:
        _log({"stage": "abort_hotkey_install", "outcome": "error", "error": repr(exc)})
        return False
