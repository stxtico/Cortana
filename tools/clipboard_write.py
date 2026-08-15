"""clipboard_write (PROMPTS.md A23) - agent-callable. No
REQUIRES_CONFIRMATION: staging text on the local clipboard is fully local
and reversible - none of CLAUDE.md rule 4's four gated categories
(delete/send/spend/submit/unlock) apply; nothing leaves the machine or
reaches another party until the user separately pastes it somewhere
themselves.
"""

from tools import _clipboard

REQUIRES_CONFIRMATION = False


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "clipboard_write",
            "description": "Replace the system clipboard's contents with the given text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The text to copy to the clipboard."},
                },
                "required": ["text"],
            },
        },
    }


async def execute(text: str) -> str:
    _clipboard.write_text(text)
    return f"Copied {len(text)} characters to the clipboard."
