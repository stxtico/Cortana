"""Shared low-level "wait for a text answer from the user" mechanism. CLI
only right now - services/brain/agent.py runs standalone, with no wiring yet
to services/ears/pipeline.py's mic/STT path, so there's no way for a spoken
answer to reach anything in here. Both services/brain/agent_safety.py's
confirm() and tools/ask_user.py's execute() call get_answer() for the actual
"block until the user responds" step - deliberately factored out so that one
piece of plumbing exists once, not two copies that would drift.

The two callers stay semantically distinct on purpose, and neither should be
made to look like the other:
- confirm() is a dispatcher-enforced gate that stops an action already
  decided - the model cannot route around it, and it answers yes/no.
- ask_user is the model choosing to ask a question before deciding anything -
  it's a normal tool call, not a gate, and it answers with free text.
They share how an answer is physically obtained, not what an answer means.

Swapping the mechanism for a real voice answer (once agent.py is wired into
services/brain/loop.py) is meant to land here, as a new handler passed to
set_input_handler() - a callback swap, not a redesign of confirm() or
ask_user.execute(). Built once, for both, at that integration step - see
CLAUDE.md's A10 entry for why this wasn't built twice now.
"""

import asyncio
from collections.abc import Awaitable, Callable

InputHandler = Callable[[str], Awaitable[str]]


async def _cli_input(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


_handler: InputHandler = _cli_input


def set_input_handler(handler: InputHandler) -> None:
    """Swaps the mechanism used by get_answer() - e.g. to a voice-based one
    once the loop-integration step lands. Everything already calling
    get_answer() picks up the new mechanism automatically."""
    global _handler
    _handler = handler


async def get_answer(prompt: str) -> str:
    return await _handler(prompt)
