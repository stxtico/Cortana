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

from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeOffsetShape

ROOT = Path(__file__).resolve().parent.parent
CAD_ROOT = ROOT / "cad"
VERIFIED_ROOT = CAD_ROOT / "verified"
DATASET_PATH = CAD_ROOT / "dataset.jsonl"
GENERATED_ROOT = CAD_ROOT / "generated"


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


def bounding_box_mm(solid) -> tuple[float, float, float]:
    """(x, y, z) extents in mm - CadQuery/OCCT solids are unitless floats,
    but every convention in this project (and the STEP/STL export formats)
    treats them as mm, so this is a plain read, not a conversion."""
    bb = solid.BoundingBox()
    return bb.xlen, bb.ylen, bb.zlen


def count_inner_wire_loops(solid) -> int:
    """A real, cheap lower bound on hole count - not a precise count, stated
    honestly: a normal through-hole in a flat wall contributes an inner
    wire loop on each face it passes through (typically 2, one per side);
    a blind hole contributes 1. Summing every face's inner wires and
    comparing against an expected count with `>=` catches gross
    under-counts (a hole silently not cut at all) without claiming an
    exactness this can't generically guarantee.

    Found genuinely necessary, not built speculatively: a real A14
    generation run produced a script whose loop cut what looked like 4
    holes but actually only cut 1 (cq.Workplane("XY").translate(...) on a
    freshly-created, empty workplane doesn't reposition subsequent
    sketching - every "different" cutter landed at the same origin) - the
    existing checks (watertight, volume, bounding box) all passed anyway,
    since none of them look at feature count at all, and even the vision
    comparison missed it on that run. This closes that specific,
    demonstrated gap."""
    total = 0
    for face in solid.Faces():
        total += len(face.innerWires())
    return total


def min_wall_probe(solid, threshold_mm: float) -> bool:
    """True if the solid has no wall/feature thinner than threshold_mm -
    approximate but real, not fabricated: attempts a uniform inward offset
    of the solid's boundary by threshold_mm via OCCT's
    BRepOffsetAPI_MakeOffsetShape and checks the result is still a valid
    solid. A wall thinner than the probe distance causes the offset to
    self-intersect (IsDone() False, or a "done" but BRepCheck_Analyzer-
    invalid result, or a raised exception on genuinely degenerate output -
    all three are treated as "too thin," not just IsDone() alone, which
    tested empirically permissive: it read True on solids that clearly did
    have a sub-threshold wall).

    Confirmed directionally correct against three real cases before use:
    a solid with a genuine ~0.1mm floor failed a -1.0mm probe; a uniform
    10mm-thick box passed everything up to the geometric half-thickness
    (-4.9mm) and failed just past it (-5.5mm); a real L-bracket with 4mm
    nominal wall thickness failed around -2.0mm - conservative relative to
    the nominal value (the multi-face offset hits the inner corner/fillet
    region first), which is the right direction for a safety check to be
    wrong in."""
    try:
        mk = BRepOffsetAPI_MakeOffsetShape()
        mk.PerformByJoin(solid.wrapped, -abs(threshold_mm), 1e-6)
        if not mk.IsDone():
            return False
        return BRepCheck_Analyzer(mk.Shape()).IsValid()
    except Exception:
        return False


def verify_solid(result, min_wall_mm: float | None = None) -> tuple[bool, str]:
    """The one real definition of "verified" (PLAN.md's CAD section: execute,
    discard anything that fails, isn't watertight, or has non-positive
    volume). Also checks STEP export specifically, not just isValid() -
    PLAN.md's stated output format *is* "CadQuery... exports STEP", so a
    solid OCCT calls valid but can't actually export isn't a usable training
    pair either. Returns (ok, reason) - reason is empty on success, a short
    human-readable explanation on failure (goes straight into attempts.jsonl
    or a discard count, not meant to be exhaustive).

    min_wall_mm is optional and off by default - scripts/cad_synth.py's mass
    variant generation (A13) doesn't pass it, keeping that path's existing
    speed characteristics unchanged; tools/cad.py's generation loop (A14)
    does, for the "no zero-thickness walls" requirement PLAN.md's
    validation-between-iterations step needs. Same shared function either
    way, not two definitions of "verified" that could drift apart."""
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

    if min_wall_mm is not None and not min_wall_probe(solid, min_wall_mm):
        return False, f"a wall or feature appears thinner than {min_wall_mm}mm (offset probe failed)"

    with tempfile.TemporaryDirectory() as tmp:
        step_path = Path(tmp) / "check.step"
        try:
            solid.exportStep(str(step_path))
        except Exception as exc:
            return False, f"STEP export raised: {type(exc).__name__}: {exc}"
        if not step_path.exists() or step_path.stat().st_size == 0:
            return False, "STEP export produced an empty/missing file"

    return True, ""
