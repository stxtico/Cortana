"""copy (PROMPTS.md A24) - ordinary file copy, write-capable, gated
(CLAUDE.md rule 4). Both source and destination resolve through
tools/_fs.py's existing write_whitelist_dirs whitelist - the same check
write_file.py/write_docx.py etc. already use, not a second path check
invented for this tool (explicit instruction). Source has to exist inside
the whitelist too, not just the destination - copying FROM an unauthorized
location would leak its contents into an authorized one just as much as
writing there directly would.
"""

import shutil

from tools import _fs

REQUIRES_CONFIRMATION = True

_WRITE_KEY = "write_whitelist_dirs"


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "copy",
            "description": (
                "Copy a file or directory. Both the source and destination must be inside "
                f"these directories (and their subdirectories): {_fs.whitelist_description(_WRITE_KEY)}. "
                "Requires user confirmation before it actually runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "Path to the file or directory to copy, absolute or relative to the project root."},
                    "dst": {"type": "string", "description": "Destination path, absolute or relative to the project root."},
                },
                "required": ["src", "dst"],
            },
        },
    }


def describe(src: str, dst: str) -> str:
    return f"Copy {src!r} to {dst!r}."


async def execute(src: str, dst: str) -> str:
    resolved_src = _fs.resolve_in_whitelist(src, _WRITE_KEY)
    resolved_dst = _fs.resolve_in_whitelist(dst, _WRITE_KEY)
    if not resolved_src.exists():
        return f"No such file or directory: {resolved_src}"

    resolved_dst.parent.mkdir(parents=True, exist_ok=True)
    if resolved_src.is_dir():
        shutil.copytree(resolved_src, resolved_dst, dirs_exist_ok=True)
    else:
        shutil.copy2(resolved_src, resolved_dst)

    return f"Copied {resolved_src} to {resolved_dst}."
