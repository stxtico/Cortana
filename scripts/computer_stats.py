"""Reads logs/computer.jsonl and reports how tools/computer.py's resolution
tiers and post-action verification are actually behaving (PROMPTS.md A25) -
not just that they're recorded, since A22 Step 3 already logs resolved_via
and verify_outcome on every action, but nothing before this read that log
back and surfaced a rate. That gap matters more now than it did before A25:
going from one hand-tested allowlisted app (explorer) to any app means the
vision (and uia_setofmark) tiers now fire on apps nobody has ever measured
real UIA coverage for, and a wrong UIA element still resolves and clicks
with full confidence (A22 Step 3's own framing) - the only way that pattern
becomes visible before it's a wrong click is looking at the aggregate rate,
which no single log line shows.

Same --since/--until/--json convention as scripts/latency_report.py, and the
same "one real implementation, not two that can drift" reasoning for
anything that might eventually want this from a UI: compute_report() is the
one function that knows the log format, everything else formats it.

    uv run scripts/computer_stats.py
    uv run scripts/computer_stats.py --since 2026-08-15T00:00:00+00:00
"""

import argparse
import json
import tomllib
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "logs" / "computer.jsonl"
CONFIG_PATH = ROOT / "config" / "cortana.toml"

UIA_TIERS = {"uia", "uia_setofmark"}
FAIL_OUTCOMES = {"unchanged", "vanished"}
# action='open' always logs resolved_via="cli" - it never goes through the
# resolution tier chain (uia/uia_setofmark/playwright/vision) at all, it's a
# structurally different action type. Counting it in the UIA-rate denominator
# below would conflate "how many times did I open something" with "how well
# is the tier chain resolving clicks" - confirmed live: a real 12-action log
# with 4/4 real click/type actions UIA-resolved (100%) plus 8 unrelated
# 'open' calls reported as 33% and fired a false warning before this
# exclusion was added.
RESOLUTION_TIER_VALUES = {"uia", "uia_setofmark", "playwright", "vision"}


def _read_jsonl(path: Path, since: datetime | None, until: datetime | None) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if since is not None or until is not None:
                ts = record.get("timestamp")
                if ts is None:
                    continue
                ts_parsed = datetime.fromisoformat(ts)
                if since is not None and ts_parsed < since:
                    continue
                if until is not None and ts_parsed > until:
                    continue
            records.append(record)
    return records


def _thresholds() -> dict:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("tools", {}).get("computer", {}).get("stats", {})


def compute_report(since: datetime | None = None, until: datetime | None = None) -> dict:
    records = [r for r in _read_jsonl(LOG_PATH, since, until) if r.get("stage") == "action"]
    thresholds = _thresholds()
    uia_rate_warn = thresholds.get("uia_rate_warn_threshold", 0.5)
    verify_fail_warn = thresholds.get("uia_verify_fail_warn_threshold", 0.3)

    overall_resolved_via = Counter()
    per_app_resolved_via: dict[str, Counter] = defaultdict(Counter)
    verify_by_tier: dict[str, Counter] = defaultdict(Counter)  # resolved_via -> outcome counts

    for r in records:
        app = r.get("app", "(unknown)")
        resolved_via = r.get("resolved_via")
        overall_resolved_via[resolved_via] += 1
        per_app_resolved_via[app][resolved_via] += 1
        outcome = r.get("verify_outcome")
        if outcome is not None:
            verify_by_tier[resolved_via][outcome] += 1

    warnings = []
    per_app_report = {}
    for app, counts in per_app_resolved_via.items():
        resolved_total = sum(v for k, v in counts.items() if k in RESOLUTION_TIER_VALUES)
        uia_count = counts.get("uia", 0) + counts.get("uia_setofmark", 0)
        uia_rate = (uia_count / resolved_total) if resolved_total else None
        per_app_report[app] = {"counts": dict(counts), "resolved_total": resolved_total, "uia_rate": uia_rate}
        if resolved_total >= 3 and uia_rate is not None and uia_rate < uia_rate_warn:
            warnings.append(
                f"{app!r}: UIA resolution rate is {uia_rate:.0%} of {resolved_total} resolved actions "
                f"(below the {uia_rate_warn:.0%} warn threshold) - the vision/uia_setofmark tiers are "
                f"carrying most of this app's clicks. Worth spot-checking verify_outcome for it below."
            )

    verify_report = {}
    for tier, outcomes in verify_by_tier.items():
        total = sum(outcomes.values())
        fail_count = sum(v for k, v in outcomes.items() if k in FAIL_OUTCOMES)
        fail_rate = fail_count / total if total else None
        verify_report[tier] = {"counts": dict(outcomes), "total": total, "fail_rate": fail_rate}
        if tier in UIA_TIERS and total >= 3 and fail_rate is not None and fail_rate >= verify_fail_warn:
            warnings.append(
                f"resolved_via={tier!r}: {fail_rate:.0%} of {total} verified actions came back "
                f"unchanged/vanished (at or above the {verify_fail_warn:.0%} warn threshold) - the exact "
                f"'a wrong UIA element still resolves and clicks with full confidence' pattern A22 Step 3 "
                f"was built to catch. Worth a closer look at logs/computer.jsonl for this tier."
            )

    return {
        "total_actions": len(records),
        "overall_resolved_via": dict(overall_resolved_via),
        "per_app": per_app_report,
        "verify_by_tier": verify_report,
        "warnings": warnings,
    }


def _print_report(report: dict, since: str | None, until: str | None) -> None:
    scope = ""
    if since and until:
        scope = f" from {since} to {until}"
    elif since:
        scope = f" since {since}"
    elif until:
        scope = f" until {until}"
    print(f"computer.py resolution/verification report{scope}")
    print(f"Total logged actions: {report['total_actions']}")
    if report["total_actions"] == 0:
        print("(no data - run some real computer actions first)")
        return

    print("\nOverall resolved_via distribution:")
    for tier, count in sorted(report["overall_resolved_via"].items(), key=lambda kv: -kv[1]):
        print(f"  {tier!r:20s} {count}")

    print("\nPer-app UIA resolution rate:")
    for app, data in sorted(report["per_app"].items()):
        rate = data["uia_rate"]
        rate_str = f"{rate:.0%}" if rate is not None else "n/a"
        print(f"  {app:20s} {rate_str:>6s} of {data['resolved_total']} resolved  {data['counts']}")

    print("\nverify_outcome by resolution tier:")
    for tier, data in sorted(report["verify_by_tier"].items()):
        fail = data["fail_rate"]
        fail_str = f"{fail:.0%}" if fail is not None else "n/a"
        print(f"  {tier!r:20s} unchanged/vanished rate {fail_str:>6s} of {data['total']}  {data['counts']}")

    if report["warnings"]:
        print("\nWARNINGS:")
        for w in report["warnings"]:
            print(f"  - {w}")
    else:
        print("\nNo warnings - nothing crossed the configured thresholds.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=str, default=None, help="ISO8601 timestamp - only include actions at or after this time.")
    parser.add_argument("--until", type=str, default=None, help="ISO8601 timestamp - only include actions at or before this time.")
    parser.add_argument("--json", action="store_true", help="print the report as JSON instead of tables")
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since) if args.since else None
    until = datetime.fromisoformat(args.until) if args.until else None

    report = compute_report(since, until)

    if args.json:
        print(json.dumps(report))
        return

    _print_report(report, args.since, args.until)


if __name__ == "__main__":
    main()
