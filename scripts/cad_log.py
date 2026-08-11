"""cad_log.py (PROMPTS.md A13) - adds a verified part to cad/verified/ and
appends failed attempts to its attempts.jsonl. Pure plumbing, no model:
every script add-part accepts is actually executed and checked
(scripts/_cad_common.py's verify_solid() - the same check cad_synth.py uses
on every generated variant) before it's copied in, so nothing unverified
lands in the library PLAN.md's synthetic-generation step builds on.

    uv run scripts/cad_log.py add-part NAME --script PATH [--description PATH_OR_TEXT]
        [--process FDM] [--material PLA] [--tolerances "..."] [--print-result "..."]
        [--param-range NAME MIN MAX] [--param-range NAME MIN MAX ...]
    uv run scripts/cad_log.py add-attempt NAME --script PATH --error "what was wrong"
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cad_common import ROOT, load_build_fn, part_dir, verify_solid  # noqa: E402


def cmd_add_part(args: argparse.Namespace) -> None:
    script_path = Path(args.script)
    if not script_path.exists():
        raise SystemExit(f"No such script: {script_path}")

    build_fn = load_build_fn(script_path)
    try:
        result = build_fn()
    except Exception as exc:
        raise SystemExit(f"Refusing to add {args.name!r}: build() raised {type(exc).__name__}: {exc}")
    ok, reason = verify_solid(result)
    if not ok:
        raise SystemExit(
            f"Refusing to add {args.name!r}: the script's default build() output failed "
            f"verification ({reason}). Nothing unverified goes into cad/verified/ - fix the "
            f"script (or log this as an attempt with add-attempt) and try again."
        )

    target = part_dir(args.name)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(script_path, target / "part.py")

    if args.description is None:
        if not (target / "description.md").exists():
            (target / "description.md").write_text("", encoding="utf-8")
    else:
        desc_path = Path(args.description)
        if desc_path.exists():
            shutil.copy2(desc_path, target / "description.md")
        else:
            (target / "description.md").write_text(args.description, encoding="utf-8")

    meta = {
        "process": args.process,
        "material": args.material,
        "tolerances": args.tolerances,
        "print_result": args.print_result,
    }
    if args.param_range:
        meta["param_ranges"] = {name: [float(lo), float(hi)] for name, lo, hi in args.param_range}
    (target / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    attempts_path = target / "attempts.jsonl"
    if not attempts_path.exists():
        attempts_path.touch()

    print(f"Added {args.name!r} to {target.relative_to(ROOT)} - verified (volume={result.val().Volume():.2f}).")


def cmd_add_attempt(args: argparse.Namespace) -> None:
    target = part_dir(args.name)
    if not target.exists():
        raise SystemExit(f"No such part {args.name!r} - run add-part first.")
    script_path = Path(args.script)
    if not script_path.exists():
        raise SystemExit(f"No such script: {script_path}")
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "script": script_path.read_text(encoding="utf-8"),
        "error": args.error,
    }
    with (target / "attempts.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    print(f"Logged failed attempt for {args.name!r} ({target / 'attempts.jsonl'}).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add-part", help="add a verified part (executes + checks the script first)")
    p_add.add_argument("name")
    p_add.add_argument("--script", required=True)
    p_add.add_argument("--description", default=None, help="path to a description.md, or inline text")
    p_add.add_argument("--process", default="FDM")
    p_add.add_argument("--material", default="")
    p_add.add_argument("--tolerances", default="")
    p_add.add_argument("--print-result", dest="print_result", default="")
    p_add.add_argument(
        "--param-range", nargs=3, metavar=("NAME", "MIN", "MAX"), action="append", default=None,
        help="explicit variant range for cad_synth.py - repeatable, one per parameter",
    )
    p_add.set_defaults(func=cmd_add_part)

    p_attempt = sub.add_parser("add-attempt", help="append a failed attempt for an existing part")
    p_attempt.add_argument("name")
    p_attempt.add_argument("--script", required=True, help="path to the failed script's source")
    p_attempt.add_argument("--error", required=True, help="what was wrong with it")
    p_attempt.set_defaults(func=cmd_add_attempt)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
