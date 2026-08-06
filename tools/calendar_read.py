"""calendar_read (PROMPTS.md A9) - read-only, so no confirmation gate (CLAUDE.md
rule 4 is about anything that deletes/sends/spends/unlocks; reading isn't
that). Dormant until Outlook is both installed AND already running - see
tools/_outlook.py for why it requires "already running" specifically (rule
10: the availability check can never launch it itself), why Outlook COM
automation is the zero-credential path, and the "not verified live" caveat.
"""

from datetime import datetime, timedelta

from tools import _outlook

_OL_FOLDER_CALENDAR = 9


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "calendar_read",
            "description": "List upcoming calendar events from the user's default Outlook calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days_ahead": {
                        "type": "integer",
                        "description": "How many days ahead to look (default 7).",
                    },
                },
                "required": [],
            },
        },
    }


async def is_available() -> bool:
    return await _outlook.is_available()


async def execute(days_ahead: int = 7) -> str:
    namespace = _outlook.get_namespace()
    calendar = namespace.GetDefaultFolder(_OL_FOLDER_CALENDAR)
    items = calendar.Items
    items.IncludeRecurrences = True
    items.Sort("[Start]")

    now = datetime.now()
    end = now + timedelta(days=days_ahead)
    restriction = "[Start] >= '{}' AND [Start] <= '{}'".format(
        now.strftime("%m/%d/%Y %H:%M %p"), end.strftime("%m/%d/%Y %H:%M %p")
    )
    upcoming = items.Restrict(restriction)

    lines = [f"- {item.Start}: {item.Subject}" for item in upcoming]
    return "\n".join(lines) if lines else f"Nothing on the calendar in the next {days_ahead} days."
