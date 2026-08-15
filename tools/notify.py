"""notify (PROMPTS.md A23) - agent-callable Windows toast
(tools/_notify.py). No REQUIRES_CONFIRMATION: a notification is a local,
ephemeral, fully reversible UI event, none of CLAUDE.md rule 4's four gated
categories (delete/send/spend/submit/unlock) - same reasoning as
tools/set_timer.py.
"""

from tools import _notify

REQUIRES_CONFIRMATION = False


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "notify",
            "description": (
                "Show a real Windows toast notification (Action Center), visible even if "
                "the user isn't looking at any particular window right now. Use for "
                "something worth surfacing that isn't itself the spoken response - e.g. "
                "flagging that a background task finished, or a reminder that should stay "
                "visible after this conversation turn moves on. Not for anything that "
                "should just be said out loud - that's the normal spoken response, not this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short toast title."},
                    "message": {"type": "string", "description": "Toast body text."},
                },
                "required": ["title", "message"],
            },
        },
    }


async def execute(title: str, message: str) -> str:
    _notify.send(title, message)
    if not _notify.toast_enabled():
        return (
            f"Notification sent ({title!r} - {message!r}), but Windows notifications are "
            "disabled system-wide on this machine (Settings > System > Notifications is "
            "off) - it will not actually be visible until that's turned back on. Mention "
            "this to the user rather than assuming it was seen."
        )
    return f"Notification shown: {title!r} - {message!r}"
