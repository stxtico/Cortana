"""Shared path-whitelist logic for read_file.py and list_dir.py - not itself an
agent-callable tool (underscore prefix), factored out so the security-relevant
check (is this path actually inside a whitelisted directory, not just prefix-
matching a string) exists in exactly one place.
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"


def _whitelist_dirs_raw() -> list[str]:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("tools", {}).get("whitelist_dirs", [])


def load_whitelist() -> list[Path]:
    return [(ROOT / d).resolve() for d in _whitelist_dirs_raw()]


def whitelist_description() -> str:
    """Project-root-relative dir names, for interpolating into a tool's SPEC
    description - the model has no way to discover the whitelist otherwise
    (confirmed live: without this, it asked for a path instead of exploring,
    even when told a config file existed somewhere)."""
    dirs = _whitelist_dirs_raw()
    return ", ".join(dirs) if dirs else "(none configured)"


def resolve_in_whitelist(path_str: str) -> Path:
    """Resolves path_str (absolute, or relative to the project root) and
    raises PermissionError unless the resolved path is a whitelisted directory
    itself or somewhere underneath one. Resolving before comparing (not just
    string-prefix matching the raw input) is what actually closes off
    `../../` traversal tricks."""
    candidate = Path(path_str)
    candidate = candidate.resolve() if candidate.is_absolute() else (ROOT / candidate).resolve()
    whitelist = load_whitelist()
    if not any(candidate == base or base in candidate.parents for base in whitelist):
        allowed = ", ".join(str(w) for w in whitelist)
        raise PermissionError(f"{candidate} is outside the configured whitelist directories ({allowed}).")
    return candidate
