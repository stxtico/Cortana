"""Daemon output - CLI now, voice later (PROMPTS.md A11). Same "callback
swap, not a redesign" shape as services/brain/user_input.py (A9/A10):
set_output_handler() swaps the mechanism once the daemon is wired to speak
through the real voice loop, without touching daemon.py's own decision
logic (trigger polling, quiet hours, rate limit, relevance filter all stay
exactly as they are).

Deferred for the same reason as A10's ask_user answer and A9's confirmation
gate: there is currently no path from this process into the live voice
loop's TTS output at all (services/brain/loop.py and services/daemon/
daemon.py are separate processes, and the loop isn't wired to accept
proactive interjections yet). Not a placeholder pretending to work - stated
plainly, same as those two.

PROMPTS.md A23 adds the toast path (tools/_notify.py) alongside whatever
_handler currently is - A11 already built triggers, the relevance filter,
and rate limiting; a toast was the missing output, not a new system. Fires
independently of _handler (not routed through set_output_handler's swap),
since a toast should keep working the same way regardless of whether the
primary mechanism is CLI print today or voice later. Real fallback, not a
silent drop: tools/_notify.py's toast_enabled() check exists precisely
because A23 caught this live - Windows notifications were globally off on
this machine (Settings > System > Notifications), and winotify's own send
call returns cleanly whether or not anything actually appears. A proactive
daemon that silently discards what it wanted to tell the user is worse than
one that just prints - so a disabled or failed toast falls back to a CLI
print of its own here, independent of _handler's current state.
"""

from collections.abc import Awaitable, Callable

from tools import _notify

OutputHandler = Callable[[str], Awaitable[None]]


async def _cli_output(text: str) -> None:
    print(f"\n[DAEMON] {text}")


_handler: OutputHandler = _cli_output


def set_output_handler(handler: OutputHandler) -> None:
    """Swaps the mechanism used by announce() - e.g. to a real voice
    callback once the daemon is wired into the live loop. Everything already
    calling announce() picks up the new mechanism automatically."""
    global _handler
    _handler = handler


async def _notify_or_fallback(text: str) -> None:
    try:
        if not _notify.toast_enabled():
            print(f"\n[DAEMON] (Windows notifications are off system-wide - see Settings > System > Notifications) {text}")
            return
        _notify.send("Cortana", text)
    except Exception as exc:
        print(f"\n[DAEMON] (toast failed: {exc}) {text}")


async def announce(text: str) -> None:
    await _handler(text)
    await _notify_or_fallback(text)
