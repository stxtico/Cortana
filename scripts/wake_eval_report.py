"""Turns a scripts/wake_calibration.py capture into a comparable report: confidence
distribution, verify pass/reject breakdown with latency, VAD stats, and a
per-utterance timeline (score + verify transcript + follow-on transcript) for the
real-vs-false-accept judgment call a human still has to make from the transcripts.

This is the standard instrument for comparing wake models - run the same read-aloud
adversary test (silence/continuous-speech period, then genuine wake-phrase period)
against each model and diff the reports. Bar set for hey_cortana vs. the hey_jarvis
+ verification-gate baseline: strictly fewer false accepts surviving verification
than the baseline's 2-of-6, and zero genuine detections wrongly rejected.
"""

import argparse
import json
import statistics as stats
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / "logs" / "wake_calibration.jsonl"


def _stats_block(label: str, values: list[float]) -> None:
    if not values:
        print(f"{label}: (none)")
        return
    print(f"{label}: n={len(values)}  min={min(values):.1f}  max={max(values):.1f}  "
          f"mean={stats.mean(values):.1f}  median={stats.median(values):.1f}")


def main(log_path: Path) -> None:
    rows = [json.loads(line) for line in log_path.open()]
    if not rows:
        raise SystemExit(f"{log_path} is empty")
    t0 = rows[0]["t"]

    wake_scores = [r for r in rows if r["stage"] == "wake_score"]
    wake_detects = [r for r in rows if r["stage"] == "wake_detect"]
    verifies = [r for r in rows if r["stage"] == "verify"]
    vad_ends = [r for r in rows if r["stage"] == "vad_end"]
    stts = [r for r in rows if r["stage"] == "stt"]
    verify_by_id = {r["utterance_id"]: r for r in verifies}
    stt_by_id = {r["utterance_id"]: r["text"] for r in stts}

    duration_s = rows[-1]["t"] - t0
    print(f"=== {log_path} ===")
    print(f"Capture duration: {duration_s:.0f}s\n")

    print("=== All-frame score distribution (listening state only) ===")
    all_scores = [r["score"] for r in wake_scores]
    if all_scores:
        print(f"n={len(all_scores)}  min={min(all_scores):.4f}  max={max(all_scores):.4f}  "
              f"mean={stats.mean(all_scores):.4f}  median={stats.median(all_scores):.4f}")
        high = sum(1 for s in all_scores if s >= 0.9)
        listening_s = len(all_scores) * (rows[1]["t"] - t0 if len(rows) > 1 else 0)
        print(f"frames >= 0.9: {high} ({100 * high / len(all_scores):.1f}%)")
    print()

    print(f"=== Wake detections: {len(wake_detects)} ===")
    verify_pass = sum(1 for v in verifies if v["passed"])
    verify_reject = sum(1 for v in verifies if not v["passed"])
    print(f"Verification: {verify_pass} passed, {verify_reject} rejected "
          f"({len(wake_detects) - len(verifies)} never reached a verify decision)\n")

    _stats_block("Verify latency (ms)", [v["latency_ms"] for v in verifies])
    _stats_block("Verify latency (ms), passed only", [v["latency_ms"] for v in verifies if v["passed"]])
    _stats_block("Verify latency (ms), rejected only", [v["latency_ms"] for v in verifies if not v["passed"]])
    print()

    _stats_block("VAD endpoint latency (ms)", [v["latency_ms"] for v in vad_ends])
    timed_out = sum(1 for v in vad_ends if v.get("timed_out"))
    print(f"VAD timeouts: {timed_out}/{len(vad_ends)}\n")

    print("=== Timeline (review transcripts to judge real vs. false accept) ===\n")
    for wd in wake_detects:
        uid = wd["utterance_id"]
        t_rel = wd["t"] - t0
        v = verify_by_id.get(uid)
        follow_on = stt_by_id.get(uid, "(no follow-on transcript - rejected or truncated)")
        print(f"[{uid}] t={t_rel:6.1f}s  wake_score={wd['score']:.3f}")
        if v:
            verdict = "PASS" if v["passed"] else "REJECT"
            print(f"       verify ({v['latency_ms']:.0f}ms, {verdict}): {v['text']!r}")
        else:
            print("       verify: (not reached - verification may be disabled)")
        print(f"       follow-on: {follow_on!r}\n")

    print("=== Judgment prompt ===")
    print("For each [id] above, read the verify/follow-on text against what was actually")
    print("said at that timestamp during the test. Classify as:")
    print("  - false accept surviving verification: verify passed but nothing resembling")
    print("    the wake phrase was actually said at that time")
    print("  - genuine wrongly rejected: verify rejected but the wake phrase WAS said")
    print("  - correct: everything else")
    print("Compare counts directly against the baseline run's numbers.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    args = parser.parse_args()
    main(args.log)
