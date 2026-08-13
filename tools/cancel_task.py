"""cancel_task (PROMPTS.md A21) - stops one delegated task, queued or
running. Real termination (services/workers/manager.py's
Process.terminate()), not a polite request a worker might ignore - the same
kind of real, code-enforced stop as the global abort hotkey, just scoped to
one task_id instead of everything.
"""

from services.workers import manager

REQUIRES_CONFIRMATION = False  # stopping work isn't itself send/delete/purchase/submit


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "cancel_task",
            "description": "Stop a delegated background task, whether it's still queued or already running.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                },
                "required": ["task_id"],
            },
        },
    }


async def execute(task_id: str) -> str:
    ok = await manager.cancel(task_id)
    return f"Cancelled {task_id!r}." if ok else f"Could not cancel {task_id!r} - no task with that id."
