"""worker_trigger - a daemon trigger source for delegated task completion
(PROMPTS.md A21) - "you ask for a batch, she starts it... and she tells you
when it's done" (the done-when, verbatim). Polls the same status files
services/workers/status.py already writes, matching services/daemon/
timers.py's exact is_available()/poll() interface so this plugs into
services/daemon/daemon.py's existing _SOURCES list with no changes to that
file's own poll/relevance/quiet-hours/rate-limit machinery.
"""

from services.workers import status


async def is_available() -> bool:
    return True  # no external dependency - always on, same as timers.py


async def poll() -> list[dict]:
    """Returns a candidate for every task currently in a terminal state
    (completed/failed/cancelled). daemon.py's own announced_ids set marks
    each candidate's id seen the instant it's found, whether or not it ends
    up announced - same "the event itself is the mark" reasoning timers.py
    already documents, which is what keeps a finished task from
    re-surfacing as a fresh candidate on every poll cycle."""
    candidates = []
    for task_id, st in status.all_statuses().items():
        state = st.get("state")
        if state not in ("completed", "failed", "cancelled"):
            continue
        worker_type = st.get("worker_type", "task")
        if state == "completed":
            summary = f"Your {worker_type} task just finished."
            detail = st.get("result", "") or "No result text was reported."
        elif state == "failed":
            summary = f"Your {worker_type} task failed."
            detail = st.get("error", "") or "No error detail was reported."
        else:
            summary = f"Your {worker_type} task was cancelled."
            detail = "Cancelled before it finished."
        candidates.append({
            "source": "worker",
            "summary": summary,
            "detail": detail,
            "id": f"worker-{task_id}-{state}",
        })
    return candidates
