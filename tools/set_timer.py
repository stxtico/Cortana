"""set_timer (PROMPTS.md A11) - agent-callable tool that persists a timer
request to a shared store services/daemon/timers.py (a genuinely separate
process) polls. This is the real, working half of A11's three trigger
sources - calendar and email stay dormant (services/daemon/calendar_trigger.py,
email_trigger.py) since neither backend exists on this machine, but a timer
set through a normal conversation turn is end-to-end today: it doesn't
depend on anything external.

Not REQUIRES_CONFIRMATION - setting a timer is trivially undoable and nowhere
near CLAUDE.md rule 4's "deletes/sends/spends/unlocks" territory.
"""

import json
import time
import tomllib
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

REQUIRES_CONFIRMATION = False


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("daemon", {}).get("timers", {})


def store_path() -> Path:
    config = _load_config()
    return ROOT / config.get("store_path", "daemon_store/timers.json")


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "set_timer",
            "description": "Set a timer that notifies the user after a given number of minutes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "number", "description": "How many minutes from now the timer should fire."},
                    "label": {"type": "string", "description": "What the timer is for, e.g. 'pasta' or 'check the print'."},
                },
                "required": ["minutes", "label"],
            },
        },
    }


async def execute(minutes: float, label: str) -> str:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    timers = []
    if path.exists():
        try:
            timers = json.loads(path.read_text())
        except json.JSONDecodeError:
            timers = []
    fire_at = time.time() + minutes * 60
    timers.append({"id": uuid.uuid4().hex[:8], "label": label, "fire_at": fire_at, "fired": False})
    path.write_text(json.dumps(timers, indent=2))
    return f"Timer set: {label!r} in {minutes:g} minute(s)."
