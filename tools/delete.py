"""delete (PROMPTS.md A24) - goes to the recycle bin via send2trash, never
os.remove/Path.unlink/shutil.rmtree - a mistaken delete (a wrong path, a
model misunderstanding) is recoverable from the recycle bin and isn't from
any of those. Write-capable, gated (CLAUDE.md rule 4) - the most
irreversible-feeling of this wave's file ops even with the recycle-bin
safety net, so it gets the same confirmation gate as the others, not a
lighter one.

Resolves through tools/_fs.py's existing write_whitelist_dirs whitelist,
same as copy.py/move.py/rename.py - deleting is scoped to exactly what's
already write-authorized, no separate "delete whitelist" invented.
"""

from send2trash import send2trash

from tools import _fs

REQUIRES_CONFIRMATION = True

_WRITE_KEY = "write_whitelist_dirs"


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "delete",
            "description": (
                "Send a file or directory to the recycle bin (never a permanent delete). "
                "Must be inside these directories (and their subdirectories): "
                f"{_fs.whitelist_description(_WRITE_KEY)}. Requires user confirmation before "
                "it actually runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file or directory to delete, absolute or relative to the project root."},
                },
                "required": ["path"],
            },
        },
    }


def describe(path: str) -> str:
    return f"Send {path!r} to the recycle bin."


async def execute(path: str) -> str:
    resolved = _fs.resolve_in_whitelist(path, _WRITE_KEY)
    if not resolved.exists():
        return f"No such file or directory: {resolved}"

    send2trash(str(resolved))

    return f"Sent {resolved} to the recycle bin."
