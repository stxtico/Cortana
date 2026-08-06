"""write_file (PROMPTS.md A9) - write-capable, gated. REQUIRES_CONFIRMATION is
read by services/brain/agent.py's dispatcher, which is what actually blocks
execution until a real confirmation is given - this flag is metadata the
dispatcher acts on, not the enforcement itself (see agent_safety.py).

Whitelisted to [tools].write_whitelist_dirs - deliberately separate from
read_file/list_dir's whitelist_dirs (tools/_fs.py) and empty by default, so no
directory is write-authorized until explicitly configured.
"""

from tools import _fs

REQUIRES_CONFIRMATION = True

_WRITE_KEY = "write_whitelist_dirs"


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write text content to a file, creating it if it doesn't exist or "
                "overwriting it if it does. Only these directories (and their "
                f"subdirectories) are write-authorized: {_fs.whitelist_description(_WRITE_KEY)}. "
                "Requires user confirmation before it actually runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file, absolute or relative to the project root."},
                    "content": {"type": "string", "description": "The full text content to write."},
                },
                "required": ["path", "content"],
            },
        },
    }


def describe(path: str, content: str) -> str:
    """Human-readable action description for the confirmation gate - generated
    by the dispatcher's code, not by the model, so the safety-relevant text
    doesn't depend on the model phrasing it correctly (see agent_safety.py)."""
    preview = content if len(content) <= 120 else content[:120] + "…"
    return f"Write {len(content)} characters to {path!r}:\n    {preview!r}"


async def execute(path: str, content: str) -> str:
    resolved = _fs.resolve_in_whitelist(path, _WRITE_KEY)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {resolved}."
