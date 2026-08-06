"""timers - the one real, working trigger source in A11 (PROMPTS.md). Polls
the same JSON store tools/set_timer.py writes to. Genuinely no external
dependency - unlike calendar/email, this can run and be verified end-to-end
today, so it's the proof the rest of the daemon (relevance filter, quiet
hours, rate limit, output) actually works.
"""

import json
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("daemon", {}).get("timers", {})


def _store_path() -> Path:
    config = _load_config()
    return ROOT / config.get("store_path", "daemon_store/timers.json")


async def is_available() -> bool:
    return True  # no external dependency - always on


async def poll() -> list[dict]:
    """Returns candidates for any timer that's due and hasn't fired yet, and
    marks them fired in the store immediately - a timer firing is itself the
    event; whether it's worth interrupting for is the relevance filter's
    job downstream, not a reason to keep re-polling the same timer."""
    path = _store_path()
    if not path.exists():
        return []
    try:
        timers = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []

    now = time.time()
    candidates = []
    changed = False
    for t in timers:
        if not t.get("fired") and t.get("fire_at", float("inf")) <= now:
            candidates.append({
                "source": "timer",
                "summary": f"Timer {t['label']!r} just went off.",
                "detail": "The user explicitly set this timer themselves, so it's always something they asked to be told about.",
                "id": f"timer-{t['id']}",
            })
            t["fired"] = True
            changed = True
    if changed:
        path.write_text(json.dumps(timers, indent=2))
    return candidates
