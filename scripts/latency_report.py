"""Reads logs/{ears,brain,voice}.jsonl and prints the per-stage latency budget
table from CLAUDE.md, actual vs target - plus verify and backchannel, which
didn't exist when the original budget was written (PROMPTS.md A4).

Each stage's actual latency comes from its own log, independently - there's no
shared turn ID across ears.jsonl/brain.jsonl/voice.jsonl to join them precisely,
so "First audio out" is reported as the sum of per-stage medians (clearly labeled
as derived, not a single measured end-to-end number) rather than overclaiming
precision a proper join would need.
"""

import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EARS_LOG = ROOT / "logs" / "ears.jsonl"
BRAIN_LOG = ROOT / "logs" / "brain.jsonl"
VOICE_LOG = ROOT / "logs" / "voice.jsonl"

# (display name, target_ms or None if this stage predates/isn't in the original
# CLAUDE.md budget table)
FIRST_AUDIO_OUT_TARGET_MS = 1150


def _read_jsonl(path: Path, since: datetime | None = None, until: datetime | None = None) -> list[dict]:
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


def _split_ttfc(voice_records: list[dict]) -> tuple[list[float], list[float]]:
    """'TTS first chunk' (ttfc_ms) is measured from speak_stream()'s entry, before
    any LLM token has arrived - for buffered_stream/hybrid/etc. that time includes
    however long the trigger condition (e.g. 2 sentences or 300 chars) took the
    LLM to satisfy, not just real TTS engine latency. Diagnosed directly (A5):
    ~1.5s of a typical 1.4-2.8s ttfc_ms was LLM generation pacing, not synthesis -
    the budget table was misattributing it to TTS.

    Splits it using tts.py's 'synthesize_call' records (since_stream_start_ms,
    same clock/origin as ttfc_ms) - log order is chronological within one
    process, and the first chunk's synthesize_call always immediately precedes
    its own ttfc, so pairing each ttfc with the most recent preceding
    synthesize_call correctly isolates: wait_for_text_ms (time to trigger) +
    engine_synth_ms (ttfc_ms - that) = ttfc_ms. Older log entries recorded before
    since_stream_start_ms existed are skipped, not zero-filled - they'd silently
    understate engine_synth_ms."""
    wait_ms: list[float] = []
    synth_ms: list[float] = []
    last_since_start: float | None = None
    for r in voice_records:
        stage = r.get("stage")
        if stage == "synthesize_call" and r.get("since_stream_start_ms") is not None:
            last_since_start = r["since_stream_start_ms"]
        elif stage == "ttfc":
            ttfc = r.get("ttfc_ms")
            if ttfc is not None and last_since_start is not None:
                wait_ms.append(last_since_start)
                synth_ms.append(max(0.0, ttfc - last_since_start))
            last_since_start = None  # consumed - don't pair with a later ttfc
    return wait_ms, synth_ms


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "median": None, "p95": None, "max": None}
    sorted_v = sorted(values)
    n = len(sorted_v)
    p95_idx = min(n - 1, int(round(0.95 * (n - 1))))
    return {"n": n, "median": statistics.median(sorted_v), "p95": sorted_v[p95_idx], "max": sorted_v[-1]}


def _print_row(name: str, s: dict, target: float | None) -> None:
    if s["n"] == 0:
        target_str = f"{target:.0f}ms" if target is not None else "--"
        print(f"{name:<38} {0:>4} {'--':>9} {'--':>9} {'--':>9} {target_str:>9} {'--':>8}")
        return
    median, p95, mx = s["median"], s["p95"], s["max"]
    if target is not None:
        status = "OK" if median <= target else "OVER"
        target_str = f"{target:.0f}ms"
    else:
        status, target_str = "--", "--"
    print(f"{name:<38} {s['n']:>4} {median:>7.1f}ms {p95:>7.1f}ms {mx:>7.1f}ms {target_str:>9} {status:>8}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since", type=str, default=None,
        help="ISO8601 timestamp (e.g. 2026-08-05T12:00:00+00:00) - only include records "
             "at or after this time. Useful for scoping the report to one real conversation "
             "instead of the full log history, which accumulates benchmark/test-script runs "
             "too (e.g. the A3 strategy comparisons).",
    )
    parser.add_argument(
        "--until", type=str, default=None,
        help="ISO8601 timestamp - only include records at or before this time. Pairs with "
             "--since to scope to one specific live-test window instead of everything since "
             "then - the log files accumulate ad-hoc diagnostic/verification calls between "
             "live tests too (direct engine.synthesize()/brain_client.stream() calls made "
             "while debugging), which --since alone can't exclude.",
    )
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since) if args.since else None
    until = datetime.fromisoformat(args.until) if args.until else None

    ears = _read_jsonl(EARS_LOG, since, until)
    brain = _read_jsonl(BRAIN_LOG, since, until)
    voice = _read_jsonl(VOICE_LOG, since, until)

    if args.since and args.until:
        scope = f" from {args.since} to {args.until}"
    elif args.since:
        scope = f" since {args.since}"
    else:
        scope = " (full history)"
    print(f"Read {len(ears)} records from {EARS_LOG.relative_to(ROOT)}, "
          f"{len(brain)} from {BRAIN_LOG.relative_to(ROOT)}, "
          f"{len(voice)} from {VOICE_LOG.relative_to(ROOT)}{scope}\n")

    def _values(records, stage, field):
        return [r[field] for r in records if r.get("stage") == stage and r.get(field) is not None]

    wake_ms = _values(ears, "wake", "latency_ms")
    vad_ms = _values(ears, "vad", "latency_ms")
    stt_ms = _values(ears, "stt", "latency_ms")
    verify_ms = _values(ears, "verify", "latency_ms")
    backchannel_ms = _values(ears, "backchannel", "latency_ms")
    ttft_ms = [r["ttft_ms"] for r in brain if r.get("ttft_ms") is not None]
    ttfc_ms = _values(voice, "ttfc", "ttfc_ms")
    wait_for_text_ms, engine_synth_ms = _split_ttfc(voice)

    critical_path = [
        ("Wake word detect", wake_ms, 50),
        ("VAD endpoint", vad_ms, 200),
        ("STT", stt_ms, 300),
        ("LLM time-to-first-token", ttft_ms, 400),
        ("TTS first chunk", ttfc_ms, 200),
    ]

    header = f"{'Stage':<38} {'n':>4} {'median':>9} {'p95':>9} {'max':>9} {'target':>9} {'status':>8}"
    print(header)
    print("-" * len(header))
    for name, vals, target in critical_path:
        _print_row(name, _stats(vals), target)

    print("-" * len(header))
    medians = [_stats(vals)["median"] for _, vals, _ in critical_path]
    if all(m is not None for m in medians):
        derived_total = sum(medians)
        status = "OK" if derived_total <= FIRST_AUDIO_OUT_TARGET_MS else "OVER"
        print(f"{'First audio out (derived: sum of medians)':<38} {'':>4} {derived_total:>7.1f}ms "
              f"{'':>9} {'':>9} {FIRST_AUDIO_OUT_TARGET_MS:>7.0f}ms {status:>8}")
    else:
        missing = [name for (name, _, _), m in zip(critical_path, medians) if m is None]
        print(f"{'First audio out (derived)':<38} insufficient data - no records yet for: {', '.join(missing)}")

    print()
    print("'TTS first chunk' breakdown (A5): the row above is measured from speak_stream()'s")
    print("entry, before any LLM token has arrived - for buffered_stream/hybrid/etc. it")
    print("includes however long the trigger condition took the LLM to satisfy, not just")
    print("real TTS engine latency. Split below via tts.py's synthesize_call records -")
    print("rows with n=0 mean no post-fix log data in this window yet (nothing to derive it from).")
    print(header)
    print("-" * len(header))
    _print_row("  waiting for LLM text (first-chunk trigger)", _stats(wait_for_text_ms), None)
    _print_row("  engine synthesis (first chunk, once triggered)", _stats(engine_synth_ms), None)

    print()
    print("New stages (not in the original CLAUDE.md budget table - added per PROMPTS.md A4):")
    print(header)
    print("-" * len(header))
    _print_row("Verify (wake confirmation)", _stats(verify_ms), None)
    _print_row("Backchannel (VAD-end -> sound starts)", _stats(backchannel_ms), None)
    print()
    print("Verify runs concurrently with recording (services/ears/pipeline.py), so it's")
    print("not additive to the happy-path critical path above on true positives - only a")
    print("very short utterance pays its residual cost. Backchannel latency is VAD-end to")
    print("the pre-rendered line starting playback, dominated by the STT call the")
    print("completeness check needs before it can decide. Neither had an official target")
    print("when CLAUDE.md's budget table was written - actuals only, no pass/fail status.")


if __name__ == "__main__":
    main()
