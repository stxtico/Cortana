"""worker_main - the subprocess entry point for kind="agent" workers
(PROMPTS.md A21). Runs services/brain/agent.py's real run_agent() loop,
scoped to exactly the tool_names configured for this worker_type
(config/cortana.toml's [workers.types.<type>].tools) - the same dispatcher,
same confirmation/credential/abort-hotkey gates as the foreground
conversation, not a second, weaker code path. "Specialization is the tool
set, not the model" - this subprocess makes the exact same [models].primary
calls the conversation loop does, against the same already-running Ollama
server, no second model resident.

    python -m services.workers.worker_main <task_id> <worker_type> <params_json>

Spawned by services/workers/manager.py with stdin=DEVNULL, deliberately -
see agent_safety.confirm()'s EOFError handling for why that's safe rather
than a hang risk: a REQUIRES_CONFIRMATION tool called from in here fails
closed (declined), it doesn't crash this process or block forever.
"""

import asyncio
import json
import sys
import time
import tomllib
from pathlib import Path

from services.brain import agent
from services.workers import status

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

_SYSTEM_PROMPT = (
    "You are executing one autonomous background task, delegated by the "
    "conversational assistant while it keeps talking to the user. Complete "
    "the task described below using only the tools available to you, then "
    "report a clear, concise summary of what you did and its outcome. "
    "There is no user available to answer questions - if something is "
    "genuinely ambiguous, make the most reasonable choice and say what you "
    "assumed, rather than asking."
)


def _load_worker_config(worker_type: str) -> dict:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("workers", {}).get("types", {}).get(worker_type, {})


async def main() -> None:
    task_id, worker_type, params_json = sys.argv[1], sys.argv[2], sys.argv[3]
    params = json.loads(params_json)
    worker_config = _load_worker_config(worker_type)
    tool_names = worker_config.get("tools", [])

    task_description = params.get("task", "")
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": task_description},
    ]

    status.write_status(task_id, state="running", worker_type=worker_type, started_at=time.time())

    try:
        chunks = []
        async for token in agent.run_agent(messages, tool_names=tool_names):
            chunks.append(token)
        result = "".join(chunks).strip()
        status.write_status(task_id, state="completed", result=result, finished_at=time.time())
    except Exception as exc:
        status.write_status(task_id, state="failed", error=str(exc), finished_at=time.time())
        raise


if __name__ == "__main__":
    asyncio.run(main())
