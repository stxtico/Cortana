"""Shared path-whitelist logic for read_file.py/list_dir.py (read) and
write_file.py (write) - not itself an agent-callable tool (underscore prefix),
factored out so the security-relevant check (is this path actually inside a
whitelisted directory, not just prefix-matching a string) exists in exactly
one place.

Read and write use separate config keys (`whitelist_dirs` vs
`write_whitelist_dirs`) and are never assumed equal - read access to a
directory doesn't imply write access to it. `write_whitelist_dirs` defaults to
empty, so nothing is write-authorized until explicitly configured (PROMPTS.md
A9, CLAUDE.md rule 4: write tools are gated).
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"


def _dirs_raw(config_key: str) -> list[str]:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("tools", {}).get(config_key, [])


def load_whitelist(config_key: str = "whitelist_dirs") -> list[Path]:
    return [(ROOT / d).resolve() for d in _dirs_raw(config_key)]


def whitelist_description(config_key: str = "whitelist_dirs") -> str:
    """Project-root-relative dir names, for interpolating into a tool's spec()
    description - the model has no way to discover the whitelist otherwise
    (confirmed live in A8: without this, it asked for a path instead of
    exploring, even when told a config file existed somewhere)."""
    dirs = _dirs_raw(config_key)
    return ", ".join(dirs) if dirs else "(none configured)"


def resolve_in_whitelist(path_str: str, config_key: str = "whitelist_dirs") -> Path:
    """Resolves path_str (absolute, or relative to the project root) and
    raises PermissionError unless the resolved path is a whitelisted directory
    itself or somewhere underneath one. Resolving before comparing (not just
    string-prefix matching the raw input) is what actually closes off
    `../../` traversal tricks."""
    candidate = Path(path_str)
    candidate = candidate.resolve() if candidate.is_absolute() else (ROOT / candidate).resolve()
    whitelist = load_whitelist(config_key)
    if not any(candidate == base or base in candidate.parents for base in whitelist):
        allowed = ", ".join(str(w) for w in whitelist)
        raise PermissionError(f"{candidate} is outside the configured whitelist directories ({allowed}).")
    return candidate
