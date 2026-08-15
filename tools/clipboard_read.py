"""clipboard_read (PROMPTS.md A23) - agent-callable, read-only."""

from tools import _clipboard

REQUIRES_CONFIRMATION = False


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "clipboard_read",
            "description": "Read the current text contents of the system clipboard.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


async def execute() -> str:
    text = _clipboard.read_text()
    if text is None:
        return "The clipboard is empty or doesn't contain text."
    return text
