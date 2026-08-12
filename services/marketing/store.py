"""store - rotation/variety history for the Ghost Typer reels pipeline
(PROMPTS.md A19). A plain JSON file, not the atomic-write-with-retry pattern
services/voice/playback_state.py etc. use - those exist for cross-process
concurrent readers (Python writer, Electron reader); this store only has one
reader/writer (services/marketing/pipeline.py's own process), same shape as
services/daemon/timers.py.

Two independent histories, both append-and-trim, both read by brief.py/
format_assign.py to enforce variety in code rather than hoping a model
self-limits (CLAUDE.md rule 4's whole premise, and A9/A10's precedent that
hard caps live in the dispatcher, not the prompt):
  - briefs: every (angle, doc_type, audience) combo generated, oldest first -
    brief.py picks the least-recently-used combo, not a random one.
  - formats: every format assigned, oldest first - format_assign.py refuses
    to repeat any of the last [marketing].format_no_repeat_window entries.
"""

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
STORE_PATH = ROOT / "marketing_store" / "history.json"

# Caps file growth - variety only ever needs to look back a small window, so
# there's no reason to keep every brief/format ever generated.
_MAX_ENTRIES = 500


def _load() -> dict:
    if not STORE_PATH.exists():
        return {"briefs": [], "formats": []}
    try:
        data = json.loads(STORE_PATH.read_text())
    except json.JSONDecodeError:
        return {"briefs": [], "formats": []}
    data.setdefault("briefs", [])
    data.setdefault("formats", [])
    return data


def _save(data: dict) -> None:
    STORE_PATH.parent.mkdir(exist_ok=True)
    STORE_PATH.write_text(json.dumps(data, indent=2))


def record_brief(angle: str, doc_type: str, audience: str, channel: str) -> None:
    data = _load()
    data["briefs"].append({
        "angle": angle,
        "doc_type": doc_type,
        "audience": audience,
        "channel": channel,
        "timestamp": time.time(),
    })
    data["briefs"] = data["briefs"][-_MAX_ENTRIES:]
    _save(data)


def recent_briefs(limit: int | None = None) -> list[dict]:
    briefs = _load()["briefs"]
    return briefs[-limit:] if limit else briefs


def record_format(format_name: str) -> None:
    data = _load()
    data["formats"].append({"format": format_name, "timestamp": time.time()})
    data["formats"] = data["formats"][-_MAX_ENTRIES:]
    _save(data)


def recent_formats(window: int) -> list[str]:
    """The last `window` assigned format names, oldest first - what
    format_assign.py checks a candidate format against before it's allowed
    to repeat."""
    formats = _load()["formats"]
    return [f["format"] for f in formats[-window:]]
