"""list_dir (PROMPTS.md A8) - read-only, whitelisted to [tools].whitelist_dirs
in cortana.toml (tools/_fs.py)."""

from tools import _fs


def spec() -> dict:
    """A function, not a static dict - the whitelist can change (cortana.toml
    edit, no restart needed elsewhere in this codebase either), and the model
    has no way to discover it except through this description. Confirmed live:
    a static, generic description ("only whitelisted directories") left the
    model with nothing to explore from - it asked for a path instead of
    listing anything, even when told a relevant file existed somewhere."""
    return {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": (
                "List the files and subdirectories in a directory. Only these "
                f"directories (and their subdirectories) are accessible: {_fs.whitelist_description()}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the directory, absolute or relative to the project root.",
                    },
                },
                "required": ["path"],
            },
        },
    }


async def execute(path: str) -> str:
    resolved = _fs.resolve_in_whitelist(path)
    if not resolved.exists():
        return f"No such directory: {resolved}"
    if not resolved.is_dir():
        return f"Not a directory: {resolved}"
    entries = sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    if not entries:
        return "(empty directory)"
    return "\n".join(f"{'[dir]  ' if e.is_dir() else '[file] '}{e.name}" for e in entries)
