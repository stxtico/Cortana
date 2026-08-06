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
"""

from collections.abc import Awaitable, Callable

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


async def announce(text: str) -> None:
    await _handler(text)
