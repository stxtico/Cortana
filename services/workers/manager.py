"""manager - spawns, tracks, and reaps delegated workers (PROMPTS.md A21).
Lives entirely in the main cortana process - the same process running
run_agent()'s foreground conversation loop. Concurrency is enforced here in
code (config-driven, [workers].max_concurrent, default 2), never left to the
model to self-limit, same discipline as every other hard cap in this
project (A9's confirmation gate, A10's ask_user cap).

Two worker kinds, config-driven per worker_type ([workers.types.<type>]):
  kind="script" - runs an existing standalone module (e.g.
    services.marketing.pipeline) as a subprocess with CLI args built from
    param_flags. Never touches agent.py's tool dispatcher, so there's
    nothing gated to reach - status is inferred from the subprocess's own
    exit code and captured stdout/stderr, since the script itself was never
    taught about this module (and didn't need to be - "python -m
    services.marketing.pipeline --n 5 already runs standalone").
  kind="agent" - runs services/workers/worker_main.py, which runs
    services/brain/agent.py's real run_agent() loop scoped to this
    worker_type's configured tools - the exact same dispatcher gates as the
    foreground conversation (confirm(), credential_violation(),
    abort-hotkey registration), not a second, weaker path.

Every spawned worker is registered with services/brain/agent_safety.py
(register_worker_process) the instant it starts, and unregistered the
instant it's reaped - this is what lets the global abort hotkey terminate
every running worker, not just the foreground tool call (a genuinely
different code path from the single-task cancel, per explicit instruction -
see agent_safety.py's own _on_abort_hotkey() docstring).
"""

import asyncio
import json
import sys
import time
import tomllib
from pathlib import Path

from services.brain import agent_safety
from services.workers import queue, status

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

# In-memory, this-process-only - live subprocess handles, used for the
# concurrency count and for cancel()'s direct terminate() call. Rebuilt
# from nothing on every process restart, same as every other in-memory
# registry in this codebase (services/daemon/daemon.py's RateLimiter,
# agent_safety.py's own _current_task). Workers spawned by a now-dead
# process are independent OS processes that keep running and keep writing
# their own status regardless - they just fall outside THIS process's kill
# switch/concurrency accounting after a restart. Scoped deliberately to
# "while cortana is running," not durable across restarts - not asked for.
_active: dict[str, "asyncio.subprocess.Process"] = {}


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f).get("workers", {})


def _worker_config(worker_type: str) -> dict:
    return _load_config().get("types", {}).get(worker_type, {})


def worker_types() -> list[str]:
    """The real, currently-configured worker types - tools/delegate_task.py
    interpolates this into its schema's enum, same "constrain the input's
    shape, don't just describe it in prose" reasoning as tools/computer.py's
    app parameter."""
    return list(_load_config().get("types", {}).keys())


def _build_command(worker_type: str, worker_config: dict, task_id: str, params: dict) -> list[str]:
    kind = worker_config.get("kind")
    if kind == "script":
        # sys.executable, not a bare "python" - guarantees the same venv
        # the parent process itself is running under, same reasoning
        # services/marketing/render.py's shutil.which("npx") fix already
        # established: don't trust a bare PATH lookup to resolve correctly.
        cmd = [sys.executable, "-m", worker_config["module"]]
        for flag in worker_config.get("param_flags", []):
            if flag in params:
                cmd += [f"--{flag}", str(params[flag])]
        return cmd
    if kind == "agent":
        return [sys.executable, "-m", "services.workers.worker_main", task_id, worker_type, json.dumps(params)]
    raise ValueError(f"worker_type {worker_type!r} has no valid 'kind' in config (got {kind!r})")


async def _start(task_id: str, worker_type: str, worker_config: dict, params: dict) -> None:
    cmd = _build_command(worker_type, worker_config, task_id, params)
    status.write_status(task_id, state="running", worker_type=worker_type, started_at=time.time())

    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(ROOT),
        # DEVNULL, deliberately: a worker must never be able to consume
        # stdin meant for a foreground ask_user/confirm prompt (A10's
        # tools/shell.py bug, in a new place) - and agent_safety.confirm()
        # now fails closed on the resulting EOFError instead of crashing.
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _active[task_id] = process
    agent_safety.register_worker_process(task_id, process)
    asyncio.create_task(_reap(task_id, worker_type, process))


async def _reap(task_id: str, worker_type: str, process: "asyncio.subprocess.Process") -> None:
    stdout, stderr = await process.communicate()
    _active.pop(task_id, None)
    agent_safety.unregister_worker_process(task_id)

    worker_config = _worker_config(worker_type)
    task = queue.get_task(task_id) or {}
    cancelled = task.get("cancel_requested", False)

    existing = status.read_status(task_id) or {}
    if worker_config.get("kind") == "agent" and existing.get("state") in ("completed", "failed"):
        # worker_main.py already wrote its own precise final state (real
        # result or real error text) - don't overwrite it with a generic
        # exit-code-only guess.
        pass
    elif cancelled:
        status.write_status(task_id, state="cancelled", finished_at=time.time())
    elif process.returncode == 0:
        status.write_status(
            task_id, state="completed", finished_at=time.time(),
            result=stdout.decode(errors="replace")[-2000:].strip(),
        )
    else:
        status.write_status(
            task_id, state="failed", finished_at=time.time(),
            error=stderr.decode(errors="replace")[-2000:].strip() or f"exited with code {process.returncode}",
        )

    await _dispatch_next()


async def _dispatch_next() -> None:
    """Pulls the next queued task off the queue once a slot frees up - "a
    task queue... and workers that pull it off," literally: a worker
    finishing is what triggers the next one starting, not a separate poll
    loop."""
    config = _load_config()
    max_concurrent = config.get("max_concurrent", 2)
    if len(_active) >= max_concurrent:
        return

    statuses = status.all_statuses()
    for task in queue.all_tasks():
        task_id = task["task_id"]
        if task.get("cancel_requested"):
            continue
        state = statuses.get(task_id, {}).get("state")
        if state not in (None, "queued"):
            continue  # already started, running, or finished
        worker_config = _worker_config(task["worker_type"])
        await _start(task_id, task["worker_type"], worker_config, task["params"])
        return  # one at a time - _reap() calls back in for the next slot


async def spawn(worker_type: str, params: dict) -> str:
    worker_config = _worker_config(worker_type)
    if not worker_config:
        raise ValueError(f"Unknown worker_type {worker_type!r} - check [workers.types] in cortana.toml.")

    task_id = queue.enqueue(worker_type, params)
    config = _load_config()
    max_concurrent = config.get("max_concurrent", 2)
    if len(_active) < max_concurrent:
        await _start(task_id, worker_type, worker_config, params)
    else:
        status.write_status(task_id, state="queued", worker_type=worker_type)
    return task_id


async def cancel(task_id: str) -> bool:
    task = queue.get_task(task_id)
    if task is None:
        return False
    queue.mark_cancel_requested(task_id)

    process = _active.get(task_id)
    if process is not None and process.returncode is None:
        process.terminate()
        return True

    # Not running yet (still queued) - mark_cancel_requested above is
    # enough; _dispatch_next() skips cancelled tasks the next time it looks.
    existing = status.read_status(task_id)
    if existing is None or existing.get("state") == "queued":
        status.write_status(task_id, state="cancelled", finished_at=time.time())
    return True


def format_one(task_id: str) -> str:
    st = status.read_status(task_id)
    task = queue.get_task(task_id)
    if st is None and task is None:
        return f"No task found with id {task_id!r}."
    state = (st or {}).get("state", "unknown")
    lines = [f"{task_id}: {state}"]
    if task:
        lines.append(f"  worker_type={task['worker_type']} params={task['params']}")
    if st and st.get("result"):
        lines.append(f"  result: {st['result']}")
    if st and st.get("error"):
        lines.append(f"  error: {st['error']}")
    return "\n".join(lines)


def format_all() -> str:
    """The "one call for the whole picture" this project's own A18 finding
    points toward (2/3 -> 3/3 reliability by replacing a multi-step chain
    with a single call) - task_status() with no argument calls this, not a
    per-task loop the model would otherwise have to run and reason through
    correctly on its own."""
    tasks = queue.all_tasks()
    if not tasks:
        return "No tasks have been delegated yet."

    statuses = status.all_statuses()
    by_state: dict[str, list[str]] = {}
    for task in tasks:
        task_id = task["task_id"]
        st = statuses.get(task_id, {})
        state = st.get("state", "queued")
        label = f"{task_id} ({task['worker_type']})"
        if st.get("result"):
            label += f" - {st['result'][:120]}"
        elif st.get("error"):
            label += f" - error: {st['error'][:120]}"
        by_state.setdefault(state, []).append(label)

    order = ["running", "queued", "completed", "failed", "cancelled"]
    lines = []
    for state in order:
        if state in by_state:
            lines.append(f"{state} ({len(by_state[state])}):")
            lines.extend(f"  {item}" for item in by_state[state])
    return "\n".join(lines)
