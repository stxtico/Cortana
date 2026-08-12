"""find_file - a follow-up to PROMPTS.md A18's chaining test. The real
agent loop measured ~2/3 reliability chaining list_dir -> list_dir -> list_dir
to find a file described only in words ("the bracket part"), not given an
exact path - the multi-step search-and-don't-give-up-early reasoning itself
was the unreliable part, not tool discoverability (both were already fixed
via spec wording, see CLAUDE.md's A18 entry). This tool removes that
reasoning chain entirely by doing the recursive walk in code: one call
replaces however many list_dir round-trips a manual search would have taken,
so "open the bracket part" becomes one find_file call plus one computer call.

Same constrain-the-shape discipline as every other read tool here: searches
only tools/_fs.py's existing read whitelist (no new directory becomes
reachable that list_dir/read_file couldn't already reach), returns paths
only, never opens or reads file contents - that's still read_file's/
computer's job, this only narrows down where to point them.
"""

import re
from pathlib import Path

from tools import _fs

ROOT = _fs.ROOT
_MAX_RESULTS = 25
_WORD_RE = re.compile(r"[a-z0-9]+")
_SKIP_DIR_NAMES = {"__pycache__"}
_SKIP_SUFFIXES = {".pyc"}


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "find_file",
            "description": (
                "Search for a file or directory by a descriptive name, not "
                "necessarily an exact filename - e.g. 'bracket part' finds "
                "cad/verified/bracket/part.py without knowing that path in "
                "advance. Walks the entire whitelisted tree in one call "
                "(directories: " + _fs.whitelist_description() + ") and "
                "returns every path whose full relative path contains all "
                "the query's words. Use this instead of a chain of list_dir "
                "calls whenever you don't already know exactly where "
                "something lives - it replaces that whole search in one "
                "step. Returns paths only, doesn't open or read anything."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A descriptive name, e.g. 'bracket part' or 'wake threshold config'.",
                    },
                },
                "required": ["query"],
            },
        },
    }


async def execute(query: str) -> str:
    words = _WORD_RE.findall(query.lower())
    if not words:
        return "Error: query must contain at least one word to search for."

    matches: list[Path] = []
    for base in _fs.load_whitelist():
        if not base.exists():
            continue
        for candidate in sorted(base.rglob("*")):
            if any(part in _SKIP_DIR_NAMES for part in candidate.parts):
                continue
            if candidate.suffix in _SKIP_SUFFIXES:
                continue
            rel = candidate.relative_to(ROOT)
            if all(word in str(rel).lower() for word in words):
                matches.append(rel)
            if len(matches) >= _MAX_RESULTS:
                break
        if len(matches) >= _MAX_RESULTS:
            break

    if not matches:
        return f"No files or directories matching {query!r} found in the whitelisted directories."

    matches.sort(key=lambda p: (len(p.parts), str(p).lower()))
    lines = [f"{'[dir]  ' if (ROOT / m).is_dir() else '[file] '}{m.as_posix()}" for m in matches]
    if len(matches) >= _MAX_RESULTS:
        lines.append(f"...(capped at {_MAX_RESULTS} results - refine the query for a narrower search)")
    return "\n".join(lines)
