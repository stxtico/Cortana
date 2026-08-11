"""Shared helpers for the CAD training-data pipeline (PROMPTS.md A13) -
scripts/cad_log.py and scripts/cad_synth.py both load a part's build()
function and check a resulting solid the same way, so there's exactly one
definition of "verified" rather than two that could quietly drift apart.

part.py contract: every part module exposes `def build(**params) -> cq.Workplane`.
Numeric keyword defaults define both the canonical part (what cad_log.py's
add-part verifies) and, implicitly, the center of cad_synth.py's variant
ranges when cad/verified/<part>/meta.json doesn't specify one explicitly.
"""

import importlib.util
import inspect
import tempfile
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
CAD_ROOT = ROOT / "cad"
VERIFIED_ROOT = CAD_ROOT / "verified"
DATASET_PATH = CAD_ROOT / "dataset.jsonl"


def part_dir(name: str) -> Path:
    return VERIFIED_ROOT / name


def load_build_fn(script_path: Path) -> Callable:
    """Dynamically loads a part script's build() function via importlib
    (real module semantics - imports resolve normally, exceptions carry
    real file/line info), not exec() against a shared namespace."""
    spec = importlib.util.spec_from_file_location(f"cad_part_{script_path.stem}", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "build"):
        raise AttributeError(f"{script_path} has no build() function - see the part.py contract in this module's docstring")
    return module.build


def default_params(build_fn: Callable) -> dict:
    """Numeric keyword defaults only - a part.py is free to take non-numeric
    parameters (a material name, a bool flag), but those aren't something
    cad_synth.py can sweep a numeric range over."""
    sig = inspect.signature(build_fn)
    return {
        name: p.default
        for name, p in sig.parameters.items()
        if p.default is not inspect.Parameter.empty and isinstance(p.default, (int, float))
    }


def verify_solid(result) -> tuple[bool, str]:
    """The one real definition of "verified" (PLAN.md's CAD section: execute,
    discard anything that fails, isn't watertight, or has non-positive
    volume). Also checks STEP export specifically, not just isValid() -
    PLAN.md's stated output format *is* "CadQuery... exports STEP", so a
    solid OCCT calls valid but can't actually export isn't a usable training
    pair either. Returns (ok, reason) - reason is empty on success, a short
    human-readable explanation on failure (goes straight into attempts.jsonl
    or a discard count, not meant to be exhaustive)."""
    try:
        solid = result.val()
    except Exception as exc:
        return False, f"no solid produced: {type(exc).__name__}: {exc}"

    try:
        valid = solid.isValid()
    except Exception as exc:
        return False, f"isValid() check itself failed: {type(exc).__name__}: {exc}"
    if not valid:
        return False, "solid is not valid/watertight"

    try:
        volume = solid.Volume()
    except Exception as exc:
        return False, f"Volume() check failed: {type(exc).__name__}: {exc}"
    if volume <= 0:
        return False, f"non-positive volume ({volume})"

    with tempfile.TemporaryDirectory() as tmp:
        step_path = Path(tmp) / "check.step"
        try:
            solid.exportStep(str(step_path))
        except Exception as exc:
            return False, f"STEP export raised: {type(exc).__name__}: {exc}"
        if not step_path.exists() or step_path.stat().st_size == 0:
            return False, "STEP export produced an empty/missing file"

    return True, ""
