"""delegate_task (PROMPTS.md A21) - starts a long-running batch job on a
background worker process, so the conversation doesn't block for minutes
while it runs. Real dispatcher-level enforcement, not persona wording:
services/workers/manager.py owns the concurrency cap, the worker-kind
routing, and (for kind="agent" workers) the exact same confirmation/
credential/abort-hotkey gates as any other tool call - "delegation is not
an escalation path" is enforced there, not here.
"""

from services.workers import manager

REQUIRES_CONFIRMATION = False  # starting a batch isn't itself send/delete/purchase/submit - whatever a worker does downstream is gated on its own terms, inside its own scoped tool set


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": (
                "Start a long-running job on a background worker process instead of "
                "blocking this conversation - e.g. rendering a batch of marketing "
                "videos, or a batch of CAD parts. Returns immediately with a task_id; "
                "use task_status to check progress later, cancel_task to stop it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_type": {
                        "type": "string",
                        "enum": manager.worker_types(),
                        "description": "Which kind of worker to run.",
                    },
                    "params": {
                        "type": "object",
                        "description": (
                            "Worker-specific parameters. For the marketing worker: "
                            '{"n": 5, "channel": "organic"}. For the cad worker: '
                            '{"task": "a plain description of what to generate"}.'
                        ),
                    },
                },
                "required": ["worker_type"],
            },
        },
    }


async def execute(worker_type: str, params: dict | None = None) -> str:
    task_id = await manager.spawn(worker_type, params or {})
    return f"Started task {task_id!r} ({worker_type}). It's running in the background - use task_status to check on it."
