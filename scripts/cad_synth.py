"""cad_synth.py (PROMPTS.md A13) - takes a verified parametric part, generates
N dimensional variants across sane ranges, executes each in CadQuery,
discards anything that fails verification (scripts/_cad_common.py's
verify_solid() - the same check cad_log.py's add-part uses, not a second
definition of "verified"), and appends the survivors to cad/dataset.jsonl as
self-contained (params, script) pairs. No model involved - correctness here
is entirely machine-checkable (PLAN.md's CAD section).

Ranges come from cad/verified/<part>/meta.json's "param_ranges". Any numeric
build() parameter without an explicit range there falls back to +/-25% of
its default value - a fallback, not the primary path, since a flat
percentage can be nonsense for some dimensions (e.g. a fillet radius that
must stay well under the material thickness) - prefer setting explicit
ranges via cad_log.py's --param-range.

    uv run scripts/cad_synth.py PART_NAME [-n 200] [--seed 42]
"""

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cad_common import DATASET_PATH, ROOT, default_params, load_build_fn, part_dir, verify_solid  # noqa: E402

FALLBACK_RANGE_FRACTION = 0.25


def _load_meta(part: Path) -> dict:
    meta_path = part / "meta.json"
    if not meta_path.exists():
        return {}
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _resolve_ranges(build_fn, meta: dict) -> dict[str, tuple[float, float]]:
    defaults = default_params(build_fn)
    explicit = meta.get("param_ranges", {})
    ranges = {}
    for name, default in defaults.items():
        if name in explicit:
            lo, hi = explicit[name]
        else:
            lo, hi = default * (1 - FALLBACK_RANGE_FRACTION), default * (1 + FALLBACK_RANGE_FRACTION)
        ranges[name] = (float(lo), float(hi))
    return ranges


def _render_variant_script(base_source: str, params: dict) -> str:
    """A standalone script for this exact variant - appends an explicit
    build(**params) call to the base part's source rather than storing
    params alongside a reference to part.py, so each cad/dataset.jsonl
    entry can reproduce its exact solid on its own even if the base
    part.py's defaults change later."""
    call_args = ", ".join(f"{k}={v!r}" for k, v in params.items())
    return f"{base_source.rstrip()}\n\nRESULT = build({call_args})\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("part", help="name under cad/verified/")
    parser.add_argument("-n", "--count", type=int, default=200, help="how many variants to attempt")
    parser.add_argument("--seed", type=int, default=None, help="for reproducible variant generation")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    part = part_dir(args.part)
    script_path = part / "part.py"
    if not script_path.exists():
        raise SystemExit(f"No such verified part {args.part!r} at {script_path}")

    build_fn = load_build_fn(script_path)
    meta = _load_meta(part)
    ranges = _resolve_ranges(build_fn, meta)
    if not ranges:
        raise SystemExit(f"{script_path} has no numeric build() parameters - nothing to vary")
    base_source = script_path.read_text(encoding="utf-8")

    verified = 0
    discard_reasons: dict[str, int] = {}
    DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DATASET_PATH.open("a", encoding="utf-8") as out:
        for _ in range(args.count):
            params = {name: round(random.uniform(lo, hi), 3) for name, (lo, hi) in ranges.items()}
            try:
                result = build_fn(**params)
                ok, reason = verify_solid(result)
            except Exception as exc:
                ok, reason = False, f"{type(exc).__name__}: {exc}"
            if not ok:
                discard_reasons[reason] = discard_reasons.get(reason, 0) + 1
                continue
            record = {
                "part": args.part,
                "params": params,
                "script": _render_variant_script(base_source, params),
                "volume": result.val().Volume(),
                "verified_at": datetime.now(timezone.utc).isoformat(),
            }
            out.write(json.dumps(record) + "\n")
            verified += 1

    failed = args.count - verified
    print(f"{verified}/{args.count} variants verified and appended to {DATASET_PATH.relative_to(ROOT)} ({failed} discarded).")
    if discard_reasons:
        print("Discard reasons:")
        for reason, count in sorted(discard_reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>4}  {reason}")


if __name__ == "__main__":
    main()
