"""rename (PROMPTS.md A24) - rename a file or directory in place (same
parent, new name only - anything that changes the parent is a move, use
move.py for that). Write-capable, gated (CLAUDE.md rule 4). Resolves through
tools/_fs.py's existing write_whitelist_dirs whitelist, same as copy.py/
move.py.
"""

from tools import _fs

REQUIRES_CONFIRMATION = True

_WRITE_KEY = "write_whitelist_dirs"


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "rename",
            "description": (
                "Rename a file or directory in place (same location, new name). Must be "
                "inside these directories (and their subdirectories): "
                f"{_fs.whitelist_description(_WRITE_KEY)}. Requires user confirmation before it "
                "actually runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file or directory to rename, absolute or relative to the project root."},
                    "new_name": {"type": "string", "description": "The new filename (not a path) - e.g. 'report_final.docx'."},
                },
                "required": ["path", "new_name"],
            },
        },
    }


def describe(path: str, new_name: str) -> str:
    return f"Rename {path!r} to {new_name!r}."


async def execute(path: str, new_name: str) -> str:
    resolved = _fs.resolve_in_whitelist(path, _WRITE_KEY)
    if not resolved.exists():
        return f"No such file or directory: {resolved}"
    if "/" in new_name or "\\" in new_name:
        return f"{new_name!r} looks like a path, not a filename - use move to change location."

    target = resolved.with_name(new_name)
    _fs.resolve_in_whitelist(str(target), _WRITE_KEY)  # target must stay inside the whitelist too
    resolved.rename(target)

    return f"Renamed {resolved} to {target}."
