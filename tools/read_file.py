"""read_file (PROMPTS.md A8) - read-only, whitelisted to [tools].whitelist_dirs
in cortana.toml (tools/_fs.py)."""

from tools import _fs


def spec() -> dict:
    """A function, not a static dict - see list_dir.py's spec() for why."""
    return {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a text file. Only files inside these "
                f"directories (and their subdirectories) are accessible: {_fs.whitelist_description()}. "
                "Use list_dir first if you don't already know the exact filename."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file, absolute or relative to the project root."},
                },
                "required": ["path"],
            },
        },
    }


async def execute(path: str) -> str:
    resolved = _fs.resolve_in_whitelist(path)
    if not resolved.exists():
        return f"No such file: {resolved}"
    if not resolved.is_file():
        return f"Not a file: {resolved}"
    return resolved.read_text(encoding="utf-8", errors="replace")
