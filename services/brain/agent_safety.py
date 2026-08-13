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

import psutil

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
    declined, not as hung forever.

    EOFError (PROMPTS.md A21): a worker subprocess (services/workers/
    worker_main.py) is spawned with stdin=DEVNULL - deliberately, so it can
    never accidentally consume input meant for a foreground ask_user/confirm
    call, the exact class of bug A10 hit once already with tools/shell.py's
    _run_wsl(). But that means input() on an unattended worker's stdin
    raises EOFError immediately, not "waits and times out" - without this
    catch, a REQUIRES_CONFIRMATION tool called from inside a worker would
    crash run_agent()'s whole loop instead of just failing the one gate
    closed. Treated identically to a timeout: declined, logged, no crash -
    the gate still exists and still runs, it just can't be answered "yes"
    from a process with no attached terminal, which is the correct, safe
    default for anything a worker wasn't explicitly cleared to do."""
    print(f"\n[CONFIRMATION NEEDED]\n{description}")
    try:
        answer = await asyncio.wait_for(user_input.get_answer("Proceed? [y/N] "), timeout=timeout_s)
    except asyncio.TimeoutError:
        print("(no response within the timeout - treating as declined)")
        _log({"stage": "confirmation", "outcome": "timeout"})
        return False
    except EOFError:
        print("(no stdin available to answer - treating as declined)")
        _log({"stage": "confirmation", "outcome": "no_stdin"})
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

# PROMPTS.md A21 - delegated workers are real OS subprocesses, not asyncio
# Tasks inside this process, so cancelling _current_task above (the
# foreground tool call) never touches them - a genuinely different code
# path from the single-task cancel A18 already verified. services/workers/
# manager.py registers a live Process handle here the moment it spawns a
# worker and unregisters it the moment that worker exits (success, failure,
# or already-terminated), so this dict only ever holds workers that are
# actually still running right now.
_worker_processes: dict[str, "asyncio.subprocess.Process"] = {}


def register_current_task(task: "asyncio.Task", loop: "asyncio.AbstractEventLoop") -> None:
    global _current_task, _current_loop
    _current_task = task
    _current_loop = loop


def clear_current_task() -> None:
    global _current_task
    _current_task = None


def register_worker_process(task_id: str, process: "asyncio.subprocess.Process") -> None:
    _worker_processes[task_id] = process


def unregister_worker_process(task_id: str) -> None:
    _worker_processes.pop(task_id, None)


def terminate_process_tree(pid: int) -> None:
    """Explicit, code-enforced recursive kill for a worker's real OS process
    tree (PROMPTS.md A21) - services/workers/manager.py's cancel() and this
    module's own _on_abort_hotkey() both call this instead of a bare
    Process.terminate() on just the top-level PID.

    Live testing found the marketing worker's render stage spawns a real,
    deep descendant tree: npx -> node -> esbuild/remotion/ffmpeg ->
    multiple chrome-headless-shell.exe instances (measured: 15 real OS
    processes, 4 of them live chrome-headless-shell.exe, mid-render). A
    single terminate() on the top-level PID happened to take the whole tree
    down cleanly in two separate live tests (0 survivors within 3s each
    time) - but that appears to be Windows Job Object inheritance from
    whatever terminal launched cortana's own process, not a guarantee this
    code makes on its own. Walking and terminating every descendant
    explicitly via psutil is what makes "the kill switch stops workers"
    code-enforced regardless of how cortana itself is launched (a bare
    background service with no terminal job object wrapping it would not
    get the same protection for free)."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    children = parent.children(recursive=True)
    procs = [parent, *children]
    for p in procs:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=3)
    for p in alive:  # terminate() didn't land in time - force it
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass


def _on_abort_hotkey() -> None:
    # Runs on the keyboard package's hook thread, not the event loop thread -
    # call_soon_threadsafe is the real cross-thread bridge (same pattern
    # services/ears/pipeline.py uses for sounddevice's mic callback) for
    # cancelling an asyncio.Task specifically. terminate_process_tree() is
    # direct OS calls (psutil wrapping TerminateProcess on Windows) with no
    # event-loop dependency at all, so it's safe to call straight from this
    # hook thread the same way tools/_computer_uia.py's focus_window()
    # already makes raw win32 calls from arbitrary threads - verified
    # empirically (PROMPTS.md A21 test, including a real deep process tree
    # under active rendering), not just assumed, since this codebase has
    # been burned by exactly this kind of unverified cross-thread
    # assumption before (A18's keyboard.send()/SetForegroundWindow
    # surprises).
    #
    # Logs every detection unconditionally, including a press that arrives
    # with nothing registered - previously silent. Without this, a genuine
    # hook failure (the press never reached this callback at all - nothing in
    # logs/agent.jsonl) and a merely mistimed one (it fired, but nothing was
    # registered) looked identical from outside the process. Only the first
    # is a real bug worth chasing.
    cancelled_foreground = False
    if _current_task is not None and _current_loop is not None and not _current_task.done():
        _current_loop.call_soon_threadsafe(_current_task.cancel)
        cancelled_foreground = True

    terminated_workers = []
    for task_id, process in list(_worker_processes.items()):
        if process.returncode is None:  # still running
            terminate_process_tree(process.pid)
            terminated_workers.append(task_id)

    if cancelled_foreground or terminated_workers:
        print(f"\n[ABORT] hotkey pressed - foreground cancelled: {cancelled_foreground}, workers terminated: {terminated_workers or 'none'}.")
        _log({"stage": "abort_hotkey", "outcome": "cancelled", "foreground": cancelled_foreground, "workers_terminated": terminated_workers})
    else:
        print("\n[ABORT] hotkey pressed - nothing registered, nothing to cancel.")
        _log({"stage": "abort_hotkey", "outcome": "no_task_registered"})


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
