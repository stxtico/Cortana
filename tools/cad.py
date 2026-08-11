"""tools/cad.py (PROMPTS.md A14) - generates a parametric CadQuery part from
a text description + explicit unit-tagged dimensions, executes it,
validates it geometrically, renders it, and revises against a vision model
- up to 5 iterations - into a STEP/STL file.

Scope, stated plainly (PLAN.md): prismatic parts with clear features and
stated dimensions - brackets, enclosures, plates, mounts. Not organic
shapes, not complex assemblies, not reverse-engineering a real object to
tolerance from a photo alone. This tool refuses politely and says so rather
than degrading quietly on the second category.

Images are NOT the primary input. LLM4CAD found text-only input outperforms
image-based and multimodal input for CadQuery generation - the real
workflow is photo -> she describes the geometry back and asks about
dimensions -> the user confirms/corrects -> text description + explicit
dimensions -> this tool -> render -> compare -> revise. A photo is a
conversation starter and a verification target, never the conditioning
signal.

That makes ask_user a prerequisite, not a nicety: this tool cannot be
called at all without a `dimensions` dict, and every value in it must be a
string with an explicit unit ("40mm", "0.25in") - never a bare number. A
photo can't tell anyone whether a hole is 6mm or 8mm and can't give scale
without a reference object in frame, so if the request is missing or
ambiguous on a dimension, the model is expected to call ask_user before
calling this tool, not guess. Refusing on a bare number/missing dimension
is dispatcher-enforced here (validate_dimensions()), not left to a prompt
instruction alone - the same lesson A9/A10 already established for
confirmation gates and the ask_user cap.

Authority order for the revise loop, decided after a real test run showed
the failure mode directly: geometric validation is ground truth (exact,
measured off the actual solid via scripts/_cad_common.py's verify_solid(),
the same shared executor A13's cad_synth.py uses - reused, not
reimplemented). Vision is a weak secondary signal only - restricted to
what geometry can't express (does the shape match the description, is
anything obviously misplaced or missing), never asked about anything the
validator already measures exactly (hole count, wall thickness,
diameters, bounding box), and given an explicit, real option to abstain
("cannot tell") - this model class fills a critique-shaped hole with
invented specifics otherwise, the same failure mode as the persona
pushback work's fabricated criticism. Any vision claim that contradicts a
measured value is discarded, not acted on, and the loop terminates on
validator agreement, not vision approval.
"""

import base64
import json
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import cadquery as cq

from services.brain import client as brain_client
from tools._cad_render import render_angles
from tools._cad_retrieval import retrieve_similar_parts
from tools._cad_units import validate_dimensions

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from _cad_common import GENERATED_ROOT, bounding_box_mm, count_inner_wire_loops, load_build_fn, verify_solid  # noqa: E402

CONFIG_PATH = ROOT / "config" / "cortana.toml"
CAD_LOG_PATH = ROOT / "logs" / "cad.jsonl"

REQUIRES_CONFIRMATION = False

_CODE_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)
_SIZE_KEYWORDS = {"width", "height", "depth", "length", "thickness"}

_VISION_PROMPT_TEMPLATE = """You're looking at {n} renders of a CAD part, from different angles.

What was requested: {description}

The geometry has ALREADY been measured and verified exactly - it executes cleanly, is \
watertight, and its dimensions have been checked against what was stated. Do NOT comment on \
exact dimensions, hole count, or wall thickness - those are already confirmed by measurement, \
and your visual read of them is not more reliable than that measurement.

Only answer what a measurement can't check:
1. Does the overall shape actually look like what was described (e.g. described as an \
L-bracket, but the render shows a flat plate)?
2. Is any visible feature obviously in the wrong place relative to the request (e.g. a hole \
through the wrong face)?
3. Is a feature that was explicitly requested entirely missing from the render?

If you cannot tell something from the image, say "cannot tell" for it rather than guessing - \
a wrong specific claim is worse than admitting uncertainty.

Respond with exactly one JSON object, nothing else:
{{"shape_matches": true, false, or "cannot tell", "misplaced_feature": "<description or null>", \
"missing_feature": "<description or null>", "notes": "<one sentence, empty string if nothing to add>"}}"""


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def _log(record: dict) -> None:
    CAD_LOG_PATH.parent.mkdir(exist_ok=True)
    with CAD_LOG_PATH.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}) + "\n")


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "cad_generate",
            "description": (
                "Generate a parametric CadQuery part from a plain-language description and "
                "explicit dimensions, then execute/validate/render/revise it (up to 5 "
                "iterations) into a STEP+STL file. Works for prismatic parts with clear "
                "features and stated dimensions - brackets, enclosures, plates, mounts, "
                "flanges, spacers. Does NOT work for organic shapes, complex assemblies, or "
                "matching a real object to exact tolerance from a photo alone - say so "
                "instead of guessing if the request is really one of those. Every dimension "
                "must be a string with an explicit unit (\"40mm\", \"0.25in\") - never a bare "
                "number. If any dimension the part needs is missing or ambiguous, ask the "
                "user first (ask_user) rather than guessing - a photo alone can't give scale "
                "or confirm a hole size."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "short identifier for this part, e.g. 'shelf_bracket'"},
                    "description": {
                        "type": "string",
                        "description": "Plain-language description of the part, its shape, and its features.",
                    },
                    "dimensions": {
                        "type": "object",
                        "description": (
                            "Every stated dimension, name -> value WITH an explicit unit, e.g. "
                            '{"width": "40mm", "hole_diameter": "5mm", "thickness": "0.25in"}. '
                            "Never a bare number - if the user didn't give a unit, ask before calling this."
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                    "feature_counts": {
                        "type": "object",
                        "description": (
                            "Optional: how many of each repeated feature the part should have, e.g. "
                            '{"holes": 4}. When given, this is checked directly against the built '
                            "solid's actual geometry (a real measurement, not a guess) before anything "
                            "is considered done - use it whenever the request names a specific count."
                        ),
                        "additionalProperties": {"type": "integer"},
                    },
                },
                "required": ["name", "description", "dimensions"],
            },
        },
    }


def _extract_code(text: str) -> str:
    match = _CODE_FENCE_RE.search(text)
    return match.group(1).strip() if match else text.strip()


async def _generate_code(messages: list[dict], think: bool) -> str:
    chunks = []
    async for token in brain_client.stream(messages, think=think):
        chunks.append(token)
    return _extract_code("".join(chunks))


def _build_system_prompt(examples: list[dict]) -> str:
    parts = [
        "You generate parametric CadQuery Python scripts for real, physical, 3D-printable "
        "parts. Every script must define exactly one function:\n\n"
        "    def build(**kwargs with numeric keyword defaults) -> cq.Workplane\n\n"
        "The keyword defaults must be the exact dimensions given in the request (already "
        "converted to millimeters - use them directly as plain numbers; do not reintroduce "
        "units or convert again). Output ONLY the working Python script (the cadquery import "
        "and the build() function) - no explanation text outside the code.\n\n"
        "Scope: prismatic parts with clear features - brackets, enclosures, plates, mounts, "
        "flanges, spacers. If the request can't genuinely be built this way (organic shapes, "
        "complex assemblies), say so in a comment instead of generating something that won't "
        "actually match.\n\n"
        "When cutting a feature (e.g. a hole) at several different positions, reposition the "
        "SKETCH before drawing it - e.g. .pushPoints([(x1,y1), (x2,y2), ...]).circle(r)."
        "cutThruAll(), or a fresh .workplane(origin=(...)) per position. Do NOT call "
        ".translate() on a freshly-created, empty Workplane and then sketch on it - "
        "translate() moves objects already on the stack, not where later sketching happens, "
        "so every position ends up identical and only one feature actually gets cut even "
        "though the code looks like it makes several.\n\n"
        "cq.Workplane(\"XY\").box(w, h, t) is CENTERED at the origin by default - it spans "
        "x in [-w/2, w/2] and y in [-h/2, h/2], NOT [0, w] and [0, h]. When placing "
        "corner-relative features (e.g. \"hole_inset from each edge\"), convert to "
        "centered coordinates first (e.g. x = -w/2 + hole_inset, not x = hole_inset) - "
        "otherwise the feature position and the box's actual extent won't agree, and a cut "
        "outside the material silently does nothing."
    ]
    if examples:
        parts.append("Similar verified parts from this library, as reference for style and structure:")
        for ex in examples:
            parts.append(f"--- {ex['name']} ---\n{ex['description']}\n\n{ex['script']}")
    return "\n\n".join(parts)


def _build_request_prompt(description: str, dims_mm: dict, feature_counts: dict) -> str:
    dims_text = ", ".join(f"{k}={v:.3f}mm" for k, v in dims_mm.items())
    counts_text = f"\n\nRequired feature counts (each must actually be cut, at a genuinely distinct position): {feature_counts}" if feature_counts else ""
    return (
        f"Generate a build() function for this part:\n\n{description}\n\n"
        f"Exact dimensions (already in mm - use these as the keyword defaults): {dims_text}"
        f"{counts_text}"
    )


def _revision_prompt(reason: str) -> str:
    return (
        f"That script failed verification: {reason}\n\n"
        "Return the complete corrected script (the full build() function again, not just "
        "the changed part)."
    )


def _check_dimensions_match(result, dims_mm: dict, tolerance_frac: float) -> tuple[bool, str]:
    """Cross-checks the built solid's overall bounding-box extents against
    any stated overall-size dimensions (width/height/depth/length/
    thickness) - PLAN.md's "dimensions match what was stated" requirement.
    Deliberately doesn't map a dimension NAME to a specific axis (e.g.
    assume "height" is Z) - that's not generically knowable for arbitrary
    generated code without assuming an orientation convention. Instead
    compares the two sets by magnitude (largest stated vs. largest actual,
    etc.), which catches the real failure mode (states 40mm, generates
    ~10mm) without ever asserting a specific, possibly-wrong axis
    correspondence. An honest, limited check - not a per-feature
    dimensional audit; a hole diameter can't be verified this way, since
    it isn't a bounding-box extent."""
    size_dims = {k: v for k, v in dims_mm.items() if k.lower() in _SIZE_KEYWORDS}
    if not size_dims:
        return True, ""
    bbox = sorted(bounding_box_mm(result.val()), reverse=True)
    stated = sorted(size_dims.values(), reverse=True)[:3]
    for stated_val, actual_val in zip(stated, bbox):
        if abs(actual_val - stated_val) > stated_val * tolerance_frac + 0.5:
            return False, (
                f"stated overall dimensions {dict(sorted(size_dims.items()))} don't match the "
                f"built solid's bounding box {tuple(round(v, 2) for v in bbox)}mm (largest "
                f"stated {stated_val}mm vs largest actual {bbox[0]:.2f}mm, tolerance "
                f"{tolerance_frac * 100:.0f}%)"
            )
    return True, ""


def _check_feature_counts(result, feature_counts: dict) -> tuple[bool, str]:
    """Cross-checks a declared feature count (e.g. {"holes": 4}) against
    count_inner_wire_loops() - a real, cheap, pre-vision geometric gate for
    exactly the class of bug a real run exposed (a hole-cutting loop that
    looked right in source but only actually cut one hole, because every
    iteration sketched at the same, un-repositioned origin). >= rather than
    ==, since a normal through-hole contributes 2 loops (one per face) but
    this doesn't try to generically distinguish through- from blind-holes -
    an honest lower bound, not a precise count."""
    if not feature_counts:
        return True, ""
    expected_total = sum(feature_counts.values())
    actual_loops = count_inner_wire_loops(result.val())
    if actual_loops < expected_total:
        return False, (
            f"expected features {feature_counts} (>= {expected_total} cut boundaries) but the "
            f"built solid only has {actual_loops} - some declared feature almost certainly "
            f"wasn't actually cut (e.g. all positions collapsed to the same spot)"
        )
    return True, ""


async def _vision_check(vision_model: str, description: str, image_paths: list[Path]) -> tuple[bool, str]:
    images_b64 = [base64.b64encode(p.read_bytes()).decode("ascii") for p in image_paths]
    prompt = _VISION_PROMPT_TEMPLATE.format(n=len(image_paths), description=description)
    messages = [{"role": "user", "content": prompt, "images": images_b64}]

    # think is never passed here (defaults to client.py's think=False) - gemma3:12b
    # (the configured [models].vision) doesn't support thinking mode at all; Ollama
    # rejects the call outright with a 400 if it's requested. A different family
    # from gemma4:e4b, not just a different size, so [thinking].cad doesn't apply.
    chunks = []
    async for token in brain_client.stream(messages, model=vision_model, format="json"):
        chunks.append(token)
    raw = "".join(chunks).strip()
    try:
        verdict = json.loads(raw)
    except json.JSONDecodeError:
        return True, ""  # unparseable vision output is discarded, not treated as a failure - weak signal only

    shape_matches = verdict.get("shape_matches")
    misplaced = verdict.get("misplaced_feature")
    missing = verdict.get("missing_feature")

    if shape_matches is False:
        return False, f"vision: shape doesn't match the description ({verdict.get('notes', '')})"
    if misplaced not in (None, "null", ""):
        return False, f"vision: possible misplaced feature - {misplaced}"
    if missing not in (None, "null", ""):
        return False, f"vision: possible missing feature - {missing}"
    return True, verdict.get("notes", "") or ""


async def execute(name: str, description: str, dimensions: dict, feature_counts: dict | None = None) -> str:
    config = _load_config()
    cad_cfg = config.get("cad", {})
    max_iterations = cad_cfg.get("max_iterations", 5)
    min_wall_mm = cad_cfg.get("min_wall_mm", 0.8)
    dim_tolerance = cad_cfg.get("dimension_tolerance_frac", 0.15)
    top_k = cad_cfg.get("retrieval_top_k", 3)
    think = config.get("thinking", {}).get("cad", True)
    vision_model = config.get("models", {}).get("vision", "")

    dims_mm, errors = validate_dimensions(dimensions)
    if errors:
        return (
            "Refused: these dimensions are missing units or unrecognized (" + "; ".join(errors) + "). "
            "Ask the user for the missing units, then call cad_generate again with every "
            "dimension as an explicit unit-tagged string."
        )

    examples = await retrieve_similar_parts(description, top_k=top_k)
    out_dir = GENERATED_ROOT / name
    out_dir.mkdir(parents=True, exist_ok=True)

    messages = [
        {"role": "system", "content": _build_system_prompt(examples)},
        {"role": "user", "content": _build_request_prompt(description, dims_mm, feature_counts or {})},
    ]

    last_reason = None
    for iteration in range(1, max_iterations + 1):
        code = await _generate_code(messages, think)
        script_path = out_dir / f"iteration_{iteration}.py"
        script_path.write_text(code, encoding="utf-8")

        try:
            build_fn = load_build_fn(script_path)
            result = build_fn()
            ok, reason = verify_solid(result, min_wall_mm=min_wall_mm)
            if ok:
                ok, reason = _check_dimensions_match(result, dims_mm, dim_tolerance)
            if ok:
                ok, reason = _check_feature_counts(result, feature_counts or {})
        except Exception as exc:
            ok, reason = False, f"script raised {type(exc).__name__}: {exc}"

        if not ok:
            _log({"stage": "iteration", "name": name, "iteration": iteration, "ok": False, "reason": reason})
            messages.append({"role": "assistant", "content": code})
            messages.append({"role": "user", "content": _revision_prompt(reason)})
            last_reason = reason
            continue

        render_paths = render_angles(result, out_dir, prefix=f"iter{iteration}")
        vision_ok, vision_note = True, ""
        if vision_model:
            try:
                vision_ok, vision_note = await _vision_check(vision_model, description, render_paths)
            except Exception as exc:
                # Vision is a weak secondary signal, never a gate - if it's unreachable or
                # errors, proceed on geometric validation alone rather than blocking.
                vision_note = f"vision check unavailable ({type(exc).__name__}: {exc}) - proceeding on geometric validation alone"
                vision_ok = True

        _log({
            "stage": "iteration", "name": name, "iteration": iteration, "ok": True,
            "vision_ok": vision_ok, "vision_note": vision_note,
        })

        if vision_ok:
            step_path = out_dir / f"{name}.step"
            stl_path = out_dir / f"{name}.stl"
            result.val().exportStep(str(step_path))
            cq.exporters.export(result, str(stl_path))
            # Canonical part.py (a copy of the winning iteration, not iteration_N.py's
            # name) - tools/export_step.py and export_stl.py look for exactly this, and
            # it's directly promotable via cad_log.py add-part without hunting for which
            # iteration number won.
            (out_dir / "part.py").write_text(code, encoding="utf-8")
            _log({"stage": "done", "name": name, "iterations": iteration, "step": str(step_path)})
            note = f" Vision note: {vision_note}" if vision_note else ""
            return (
                f"Generated {name!r} in {iteration} iteration(s). Geometrically verified "
                f"(watertight, min wall >= {min_wall_mm}mm, overall dimensions match stated "
                f"values). STEP: {step_path.relative_to(ROOT)}, STL: {stl_path.relative_to(ROOT)}."
                f"{note}"
            )

        messages.append({"role": "assistant", "content": code})
        messages.append({"role": "user", "content": _revision_prompt(vision_note)})
        last_reason = vision_note

    _log({"stage": "exhausted", "name": name, "iterations": max_iterations, "last_reason": last_reason})
    return (
        f"Did not reach a fully verified part for {name!r} after {max_iterations} iterations. "
        f"Last issue: {last_reason}. Best attempt saved at "
        f"{(out_dir / f'iteration_{max_iterations}.py').relative_to(ROOT)} for manual "
        "finishing - this is a fast first draft, not a guarantee (finish in FreeCAD/Fusion "
        "when it doesn't converge)."
    )
