"""queue - the durable, main-process-owned task list for delegation
(PROMPTS.md A21). Single-writer-per-file, same discipline as
services/character/walk_signal.py's request/status file pair: this file
(workers_store/queue.json) is written ONLY by the main cortana process
(delegate_task enqueues, cancel_task sets a cancel flag), never by a worker
subprocess. Per-task live state (running/completed/failed, progress,
result) lives in status.py's own per-task files instead, each owned by
exactly one writer - splitting responsibility this way, rather than one
shared file two different processes both write to, is what keeps every
write here safe without needing atomic-write-with-retry: there is never a
second writer to race against.
"""

import json
import time
import tomllib
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

# Caps file growth - same reasoning as services/marketing/store.py's
# _MAX_ENTRIES, this only ever needs to look back far enough to show recent
# activity, not every task ever delegated.
_MAX_ENTRIES = 200


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f).get("workers", {})


def _queue_path() -> Path:
    config = _load_config()
    return ROOT / config.get("store_dir", "workers_store") / "queue.json"


def _load() -> list[dict]:
    path = _queue_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return []


def _save(tasks: list[dict]) -> None:
    path = _queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tasks[-_MAX_ENTRIES:], indent=2))


def enqueue(worker_type: str, params: dict) -> str:
    task_id = f"{worker_type}-{uuid.uuid4().hex[:8]}"
    tasks = _load()
    tasks.append({
        "task_id": task_id,
        "worker_type": worker_type,
        "params": params,
        "enqueued_at": time.time(),
        "cancel_requested": False,
    })
    _save(tasks)
    return task_id


def mark_cancel_requested(task_id: str) -> bool:
    tasks = _load()
    for t in tasks:
        if t["task_id"] == task_id:
            t["cancel_requested"] = True
            _save(tasks)
            return True
    return False


def all_tasks() -> list[dict]:
    return _load()


def get_task(task_id: str) -> dict | None:
    for t in _load():
        if t["task_id"] == task_id:
            return t
    return None
