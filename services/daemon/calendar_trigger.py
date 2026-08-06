"""calendar_trigger - one of A11's two dormant trigger sources (PROMPTS.md).
Same self-gating pattern as A8's web_search and A9's shell: built against the
real Outlook COM automation, but is_available() honestly returns False on
this machine (no Outlook running - see tools/_outlook.py for why "already
running" specifically, rule 10) so poll() never actually executes here. It
starts working automatically the moment Outlook is open, no code change.

Zero credentials, same as tools/calendar_read.py - COM automation rides
whatever account is already signed into the desktop app.
"""

import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from tools import _outlook

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

_OL_FOLDER_CALENDAR = 9


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("daemon", {}).get("calendar", {})


async def is_available() -> bool:
    return await _outlook.is_available()


async def poll() -> list[dict]:
    """Not verified live - dormant on this machine, nothing here to test
    against (see module docstring). Built against the same win32com pattern
    already used, and verified working, in tools/calendar_read.py."""
    config = _load_config()
    lookahead_minutes = config.get("lookahead_minutes", 20)
    namespace = _outlook.get_namespace()
    calendar = namespace.GetDefaultFolder(_OL_FOLDER_CALENDAR)
    items = calendar.Items
    items.IncludeRecurrences = True
    items.Sort("[Start]")

    now = datetime.now()
    window_end = now + timedelta(minutes=lookahead_minutes)
    restriction = "[Start] >= '{}' AND [Start] <= '{}'".format(
        now.strftime("%m/%d/%Y %H:%M %p"), window_end.strftime("%m/%d/%Y %H:%M %p")
    )
    upcoming = items.Restrict(restriction)

    candidates = []
    for item in upcoming:
        candidates.append({
            "source": "calendar",
            "summary": f"Calendar event {item.Subject!r} starting soon.",
            "detail": f"Starts at {item.Start}, within the configured {lookahead_minutes}-minute lookahead window.",
            "id": f"calendar-{item.EntryID}",
        })
    return candidates
