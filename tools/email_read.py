"""email_read (PROMPTS.md A9) - read-only, so no confirmation gate (same
reasoning as calendar_read.py). Dormant until Outlook is both installed AND
already running - see tools/_outlook.py for why, the zero-credential
rationale, and the "not verified live" caveat.
"""

from tools import _outlook

_OL_FOLDER_INBOX = 6


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "email_read",
            "description": "List recent messages from the user's Outlook inbox (subject, sender, received time - not the full body).",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "How many recent messages to list (default 10).",
                    },
                },
                "required": [],
            },
        },
    }


async def is_available() -> bool:
    return await _outlook.is_available()


async def execute(count: int = 10) -> str:
    namespace = _outlook.get_namespace()
    inbox = namespace.GetDefaultFolder(_OL_FOLDER_INBOX)
    items = inbox.Items
    items.Sort("[ReceivedTime]", True)

    lines = []
    for item in list(items)[:count]:
        lines.append(f"- {item.ReceivedTime} | {item.SenderName}: {item.Subject}")
    return "\n".join(lines) if lines else "No messages in the inbox."
