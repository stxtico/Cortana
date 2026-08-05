"""Rolling-context compression: folds the oldest chunk of raw conversation into
the live summary. Uses [models].primary via services/brain/client.py (no separate
resident fast model - see [memory]'s comment in cortana.toml for the VRAM
measurement behind that call), non-streaming since nothing is waiting on this
text - it's assembled in full before manager.py ever reads it.
"""

from services.brain import client as brain_client

_PROMPT = """You are compressing part of an ongoing conversation into a short, \
dense summary for your own later reference - not a response to the user, and \
nothing you write here is ever spoken aloud. Preserve concrete facts, decisions, \
and open threads; drop small talk and pleasantries. Write it as plain prose, a \
paragraph or two at most.

{previous}
New material to fold in:
{chunk}

Updated summary:"""


def _format_chunk(chunk: list[dict]) -> str:
    lines = []
    for message in chunk:
        role = "User" if message["role"] == "user" else "You"
        lines.append(f"{role}: {message['content']}")
    return "\n".join(lines)


async def summarize_chunk(previous_summary: str, chunk: list[dict]) -> str:
    previous = f"Existing summary so far:\n{previous_summary}\n" if previous_summary else ""
    prompt = _PROMPT.format(previous=previous, chunk=_format_chunk(chunk))
    pieces = []
    async for token in brain_client.stream([{"role": "user", "content": prompt}], think=False):
        pieces.append(token)
    return "".join(pieces).strip()
