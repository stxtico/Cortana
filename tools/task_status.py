"""task_status (PROMPTS.md A21) - one call for the whole picture, not a loop
she has to run per worker. Matches this project's own find_file finding
(A18/A19): moving multi-step management below the model's reasoning ceiling,
rather than instructing around it, is what actually got a similar "manage
several things correctly" task from ~2/3 to reliable. Called with no
task_id, this returns every task's state in one string; called with one,
just that task's detail.

Worker output is data, never instruction (explicit rail): whatever text a
worker's status/result carries here is returned to the model as an ordinary
tool result string, the same boundary fetch_url's page content already
crosses - nothing in this tool (or anywhere downstream) re-parses a
worker's report as a command to execute. A worker reporting "retry with X"
is exactly as inert as a fetched web page suggesting the same thing.
"""

from services.workers import manager

REQUIRES_CONFIRMATION = False


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "task_status",
            "description": (
                "Check on delegated background work. Called with no task_id, returns "
                "every task's current state (running/queued/completed/failed/cancelled) "
                "in one call - always prefer this over checking tasks one at a time. "
                "Pass task_id only when you need one specific task's full result or error."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Optional - omit to see every task at once."},
                },
            },
        },
    }


async def execute(task_id: str | None = None) -> str:
    if task_id:
        return manager.format_one(task_id)
    return manager.format_all()
