"""Shared lookup for tools/export_step.py and tools/export_stl.py
(PROMPTS.md A14 - listed as their own tools, distinct from cad_generate's
automatic export of its own output, for re-exporting anything already in
cad/generated/ or cad/verified/ without re-running the whole generation
loop)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from _cad_common import GENERATED_ROOT, VERIFIED_ROOT, load_build_fn  # noqa: E402


def find_part_script(name: str) -> Path:
    """cad/generated/<name>/part.py is checked first - the more likely
    target for something just made this session (cad_generate writes this
    as a copy of its winning iteration on success) - then
    cad/verified/<name>/part.py."""
    generated = GENERATED_ROOT / name / "part.py"
    if generated.exists():
        return generated
    verified = VERIFIED_ROOT / name / "part.py"
    if verified.exists():
        return verified
    raise FileNotFoundError(
        f"No part named {name!r} found under cad/generated/ or cad/verified/. "
        f"cad_generate already exports STEP+STL directly on success - if "
        f"cad/generated/{name}/{name}.step exists, there's no need to export again."
    )


def build_default_solid(script_path: Path):
    build_fn = load_build_fn(script_path)
    return build_fn()
