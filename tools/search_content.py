"""search_content (PROMPTS.md A23) - grep across the whitelisted
directories (tools/_fs.py, the same [tools].whitelist_dirs read_file/
list_dir already use). "Find the invoice with that number in it" currently
has no answer without this - read_file/list_dir only work once the exact
filename is already known.

Literal, case-insensitive substring search, not regex - the realistic case
("find the file with this phrase/number/name in it") doesn't need regex,
and a literal search can't accidentally misinterpret special characters in
whatever's actually being searched for (an invoice number, an email
address). Binary and oversized files are skipped rather than scanned - a
null byte in the first few KB is treated as "not text," and anything over
[tools.search_content].max_file_size_bytes is skipped outright, both so a
stray large/binary file in a whitelisted directory can't make one call slow
or return garbage matches.
"""

import tomllib
from pathlib import Path

from tools import _fs

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

REQUIRES_CONFIRMATION = False


def _config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f).get("tools", {}).get("search_content", {})


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "search_content",
            "description": (
                "Search the CONTENTS of files (not just filenames) for a literal piece of "
                f"text, across these whitelisted directories: {_fs.whitelist_description()}. "
                "Use this when a phrase, number, or name should be inside a file but you "
                "don't know which file - e.g. 'find the invoice with 12345 in it'. For "
                "finding a file by its NAME instead, use find_file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The literal text to search for (case-insensitive)."},
                    "dir": {"type": "string", "description": "Optional: restrict the search to one whitelisted directory instead of all of them."},
                },
                "required": ["query"],
            },
        },
    }


def _is_probably_text(path: Path, sniff_bytes: int = 2048) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(sniff_bytes)
    except OSError:
        return False
    return b"\x00" not in chunk


async def execute(query: str, dir: str | None = None) -> str:
    if dir:
        try:
            roots = [_fs.resolve_in_whitelist(dir)]
        except PermissionError as exc:
            return f"Error: {exc}"
    else:
        roots = _fs.load_whitelist()

    config = _config()
    max_file_size = config.get("max_file_size_bytes", 5_000_000)
    max_results = config.get("max_results", 50)

    query_l = query.lower()
    matches = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if len(matches) >= max_results:
                break
            if not path.is_file():
                continue
            try:
                if path.stat().st_size > max_file_size:
                    continue
            except OSError:
                continue
            if not _is_probably_text(path):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if query_l in line.lower():
                    try:
                        rel = path.relative_to(ROOT)
                    except ValueError:
                        rel = path
                    matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(matches) >= max_results:
                        break
        if len(matches) >= max_results:
            break

    if not matches:
        return f"No matches for {query!r} in {', '.join(str(r) for r in roots)}."
    header = f"{len(matches)} match(es) for {query!r}" + (" (capped)" if len(matches) >= max_results else "") + ":\n"
    return header + "\n".join(matches)
