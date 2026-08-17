"""session_trigger - a daemon trigger source for "a new services/brain/loop.py
session just started" (a session-start greeting is another trigger, not a
new system, per explicit instruction). Same is_available()/poll() interface
as every other module in services/daemon/_SOURCES - timers.py, worker_trigger.py
- so it plugs into daemon.py's existing poll loop with no changes to that
file's own machinery beyond the one explicit bypass branch documented in
daemon.py itself (a greeting isn't an interruption, so relevance/quiet-hours/
rate-limit don't apply to it the way they do to every other source).

Detects a new session via services/memory/session_state.py's cross-process
marker file (written by MemoryManager.__post_init__, the same place
session_id is already generated) - compares against the last session_id
THIS module has already composed a greeting for, in-process memory, same
"resets on daemon restart, and that's fine" acceptance the other in-memory
daemon state (RateLimiter, announced_ids) already documents.

Composes the greeting via the same Ollama server every other LLM call in
this project uses (services/brain/client.py) - genuinely varied per call
(temperature + real, different context each time: time of day, which
workers if any just finished, which timers if any are still pending) rather
than a fixed template with slots, per explicit instruction that an identical
greeting every launch becomes wallpaper within a week.

Deliberately does NOT load config/persona.md - the daemon stays lightweight
by design (module docstring, daemon.py), and a short tone instruction inline
below is enough for a 1-2 sentence greeting; it doesn't need the full
persona file's ~3300 tokens of response-shape/register rules that govern a
whole conversation.
"""

import time as _time

from services.brain import client as brain_client
from services.daemon import timers
from services.memory import session_state
from services.workers import status

_MAX_MENTIONED_WORKERS = 3

_last_composed_session_id: str | None = None


async def is_available() -> bool:
    return True  # no external dependency - always on, same as timers.py/worker_trigger.py


def _time_of_day_bucket(hour: int) -> str:
    if hour < 5:
        return "very late at night"
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    if hour < 21:
        return "evening"
    return "late evening"


def _recent_finished_workers() -> list[tuple[str, dict]]:
    """Most recently finished/failed/cancelled tasks, newest first - reuses
    services/workers/status.py's existing query (the same one worker_trigger.py
    already polls), not a new capability. Bounded so a long history of old
    finished tasks doesn't turn the greeting into a status report."""
    terminal = [
        (task_id, st)
        for task_id, st in status.all_statuses().items()
        if st.get("state") in ("completed", "failed", "cancelled")
    ]
    terminal.sort(key=lambda item: item[1].get("finished_at") or 0, reverse=True)
    return terminal[:_MAX_MENTIONED_WORKERS]


async def _compose_greeting(time_bucket: str, finished: list[tuple[str, dict]], pending: list[dict]) -> str:
    facts = [f"It's {time_bucket} for the user right now."]
    if finished:
        for task_id, st in finished:
            state = st.get("state")
            worker_type = st.get("worker_type", "task")
            facts.append(f"A {worker_type} task ({state}) finished while she was away: {st.get('result') or st.get('error') or '(no detail)'}")
    else:
        facts.append("No delegated tasks finished since she last spoke to the user.")
    if pending:
        facts.append(f"{len(pending)} timer(s) are still waiting to fire: {', '.join(t.get('label', '?') for t in pending)}.")
    else:
        facts.append("No timers are currently pending.")

    prompt = (
        "You are composing ONE short spoken greeting for a voice assistant that was just launched. "
        "Tone: warm but brief, not corporate, not gushing - a real \"hey, here's what's up\" from someone "
        "who's glad to be back, not a customer-service opener. 1-2 short sentences, spoken out loud, no lists, "
        "no markdown, no emoji.\n\n"
        "Facts you can draw on (only mention something if it's actually true - never invent a status):\n"
        + "\n".join(f"- {f}" for f in facts)
        + "\n\nIf nothing is waiting (no finished tasks, no pending timers), just greet naturally for the time "
        "of day - don't force a mention of \"nothing's happening.\" Vary your phrasing - this greeting is heard "
        "every time the assistant launches, so don't default to the same opening words every time.\n\n"
        "Output ONLY the greeting itself, nothing else."
    )
    chunks = []
    async for token in brain_client.stream([{"role": "user", "content": prompt}], think=False):
        chunks.append(token)
    return "".join(chunks).strip()


async def poll() -> list[dict]:
    global _last_composed_session_id
    state = session_state.read()
    if state is None:
        return []
    session_id = state["session_id"]
    if session_id == _last_composed_session_id:
        return []  # already composed a greeting for this session - don't re-run the LLM call every poll tick
    _last_composed_session_id = session_id

    time_bucket = _time_of_day_bucket(_time.localtime().tm_hour)
    finished = _recent_finished_workers()
    pending = timers.pending_timers()
    text = await _compose_greeting(time_bucket, finished, pending)

    return [{
        "source": "session",
        "summary": text,
        "detail": "A new services/brain/loop.py session just started - a greeting, not an interruption.",
        "id": f"session-{session_id}",
        # Consumed only by daemon.py's session-start bypass branch: which
        # worker_trigger candidate ids this greeting already covered, so
        # that source doesn't separately re-announce the same finished
        # tasks a moment later (the exact double-announcement risk noted
        # when this design was decided).
        "mentioned_worker_ids": [f"worker-{task_id}-{st.get('state')}" for task_id, st in finished],
    }]
