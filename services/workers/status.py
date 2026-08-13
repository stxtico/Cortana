"""status - per-task live state for delegation (PROMPTS.md A21). One
atomically-written file per task_id under workers_store/status/ - the
fourth instance of this project's established Python<->cross-process atomic
tmp+rename+retry pattern (services/voice/playback_state.py,
services/ears/listening_state.py, services/character/walk_signal.py), not a
new mechanism.

Single-writer-per-file, per task: whichever process is actually running a
given task_id writes its own status file exclusively - services/workers/
manager.py for kind="script" workers (it watches the subprocess directly;
the subprocess itself, e.g. services/marketing/pipeline.py, knows nothing
about this module and never needed to change), services/workers/
worker_main.py for kind="agent" workers (writes its own progress from
inside the subprocess it runs as). Every reader (the task_status tool, the
daemon's worker_trigger source) only ever reads.
"""

import json
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"


def _status_dir() -> Path:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f).get("workers", {})
    return ROOT / config.get("store_dir", "workers_store") / "status"


def _status_path(task_id: str) -> Path:
    return _status_dir() / f"{task_id}.json"


def write_status(task_id: str, **fields) -> None:
    """Merges fields into the existing record (if any) and writes it back -
    callers pass only what changed (e.g. just state="running"), not the
    whole record every time. Same tmp+rename-with-retry as
    playback_state.py's _write() - same real Windows os.replace()-requires-
    exclusive-access race that module's docstring documents applies here
    too."""
    path = _status_path(task_id)
    existing = read_status(task_id) or {"task_id": task_id}
    existing.update(fields)
    existing["updated_at"] = time.time()

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(existing))
    for attempt in range(3):
        try:
            tmp.replace(path)
            return
        except OSError as exc:
            if attempt == 2:
                print(f"workers.status: write failed after retries, dropping this update: {exc}", file=sys.stderr)
                tmp.unlink(missing_ok=True)
                return
            time.sleep(0.005 * (attempt + 1))


def read_status(task_id: str) -> dict | None:
    try:
        return json.loads(_status_path(task_id).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def all_statuses() -> dict[str, dict]:
    status_dir = _status_dir()
    if not status_dir.exists():
        return {}
    result = {}
    for path in status_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        result[path.stem] = data
    return result
