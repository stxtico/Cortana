"""pipeline - the Ghost Typer reels pipeline end-to-end (PROMPTS.md A19):
brief -> script -> format assignment -> render -> still verification ->
format verification -> publish, for a batch. CLI entry point matching
PROMPTS.md's own done-when: "one command produces a verified batch of MP4s
awaiting approval."

    uv run python -m services.marketing.pipeline --n 5 --channel organic
    uv run python -m services.marketing.pipeline --n 3 --channel paid

Every stage logs to logs/marketing.jsonl, same structured-JSON-per-service
convention as every other service in this project (CLAUDE.md rule 3).
Sequential, not parallel, per video - render.py's calls are GPU-bound
(headless Chromium + NVENC), and this project has already measured
concurrent GPU work contending rather than overlapping (A3's XTTS finding),
so a batch runs one video through the whole chain before starting the next.
"""

import asyncio
import json
import sys
import tomllib
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

from services.brain import client as brain_client
from services.marketing import brief, format_assign, publish, render, script, verify_format, verify_still

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"
LOG_PATH = ROOT / "logs" / "marketing.jsonl"


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f).get("marketing", {})


def _json_default(obj):
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


def _log(record: dict) -> None:
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}, default=_json_default) + "\n")


def _video_id(b: brief.Brief) -> str:
    return f"{b.angle}-{b.doc_type}-{uuid.uuid4().hex[:8]}"


async def produce_video(channel: str) -> dict:
    """Runs one video through the full chain. Never raises - every stage's
    failure is caught, logged, and reflected in the returned summary so one
    bad video doesn't kill the rest of a batch run via produce_batch()."""
    b = brief.generate_brief(channel)
    _log({"stage": "brief", "angle": b.angle, "doc_type": b.doc_type, "audience": b.audience, "channel": b.channel})

    summary = {"brief": asdict(b), "video_id": None, "format": None, "vision": None, "format_check": None, "publish": None, "ok": False, "error": None}

    try:
        script_data = await script.generate_script(b)
    except Exception as exc:
        summary["error"] = f"script generation failed: {exc}"
        _log({"stage": "script_failed", "angle": b.angle, "doc_type": b.doc_type, "error": str(exc)})
        return summary

    video_id = _video_id(b)
    summary["video_id"] = video_id
    _log({"stage": "script", "video_id": video_id, **script_data})

    fa = format_assign.assign_format(script_data, b, video_id)
    summary["format"] = fa.format_name
    _log({"stage": "format", "video_id": video_id, "format": fa.format_name, "pool_exhausted": fa.pool_exhausted})
    if fa.pool_exhausted:
        print(f"  [WARNING] format pool exhausted (every configured format used within the no-repeat window) - "
              f"consider adding a new format component per SKILL.md's 'Add a new unique format' section.")

    try:
        longest_text_path, payoff_path = await render.render_verification_stills(fa, video_id)
    except Exception as exc:
        summary["error"] = f"still render failed: {exc}"
        _log({"stage": "still_render_failed", "video_id": video_id, "error": str(exc)})
        return summary

    # Vision is a signal, never a verdict (explicit instruction) - logged
    # regardless of what it claims, never blocks the batch on its own.
    vision_verdict = await verify_still.check_stills(longest_text_path, payoff_path)
    summary["vision"] = vision_verdict
    _log({"stage": "vision_check", "video_id": video_id, **vision_verdict})
    if verify_still.flagged(vision_verdict):
        print(f"  [FLAG] vision noted a possible issue on {video_id}: {vision_verdict['notes']!r} - not a verdict, review the stills.")

    render_result = await render.render_video(fa, video_id)
    if not render_result.ok:
        summary["error"] = f"full render failed: {render_result.stderr[-500:]}"
        _log({"stage": "render_failed", "video_id": video_id, "error": render_result.stderr[-2000:]})
        return summary

    # ffprobe IS a hard gate (unlike vision) - width/height/pix_fmt are
    # exact facts, not a guess. Fail loudly, per PLAN.md's own words.
    format_check = await verify_format.check_format(render_result.mp4_path)
    summary["format_check"] = asdict(format_check)
    _log({"stage": "format_check", "video_id": video_id, **asdict(format_check)})
    if not format_check.ok:
        summary["error"] = f"format check failed: {format_check.error}"
        print(f"  [FAILED] {video_id}: {format_check.error}")
        return summary

    config = _load_config()
    if not publish.is_configured():
        summary["publish"] = {"skipped": True, "reason": "SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY not set in cortana's own .env - publish stage dormant."}
        _log({"stage": "publish_skipped", "video_id": video_id, "reason": "not configured"})
    else:
        publish_live = config.get("publish_live", False)
        try:
            media_url = await publish.upload_video(render_result.mp4_path, video_id, dry_run=not publish_live)
            row = publish.build_row(b, script_data, fa.format_name, video_id, media_url)
            if publish_live:
                result = await publish.queue_post(row)
                summary["publish"] = {"inserted": True, "content_posts_id": result.content_posts_id, "row": result.row}
                _log({"stage": "published", "video_id": video_id, "content_posts_id": result.content_posts_id})
            else:
                summary["publish"] = {"inserted": False, "dry_run": True, "row": row}
                _log({"stage": "publish_dry_run", "video_id": video_id, "row": row})
        except Exception as exc:
            summary["publish"] = {"error": str(exc)}
            _log({"stage": "publish_failed", "video_id": video_id, "error": str(exc)})

    summary["ok"] = True
    return summary


async def produce_batch(n: int, channel: str) -> list[dict]:
    results = []
    for i in range(n):
        print(f"[{i + 1}/{n}] generating ({channel})...")
        result = await produce_video(channel)
        status = "OK" if result["ok"] else "FAILED"
        print(f"  -> {status} {result['video_id'] or '(no id - failed early)'} [{result['format']}]" + (f" - {result['error']}" if result["error"] else ""))
        results.append(result)
    return results


def _print_report(results: list[dict], publish_live: bool) -> None:
    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    flagged = [r for r in ok if r["vision"] and verify_still.flagged(r["vision"])]
    published = [r for r in ok if r["publish"] and r["publish"].get("inserted")]

    print("\n=== Batch report ===")
    print(f"Verified: {len(ok)}/{len(results)}")
    if failed:
        print(f"Failed: {len(failed)} - {[r['video_id'] or r['brief']['angle'] for r in failed]}")
    if flagged:
        print(f"Vision-flagged (signal, not verdict - review before approving): {len(flagged)} - {[r['video_id'] for r in flagged]}")
    if publish_live:
        print(f"Published to content_posts (status='draft', awaiting approval at /admin/content): {len(published)}")
    else:
        print(f"publish_live is false - no real content_posts writes. Dry-run rows below, review before flipping the flag:")
        for r in ok:
            if r["publish"] and "row" in r["publish"]:
                print(f"\n  --- {r['video_id']} ---")
                print(json.dumps(r["publish"]["row"], indent=2))
    print(f"\nRendered MP4s: {[r['video_id'] + '.mp4' for r in ok]}")
    print("Awaiting approval at /admin/content once queued.")


async def main() -> None:
    args = sys.argv[1:]
    n = 5
    channel = "organic"
    if "--n" in args:
        n = int(args[args.index("--n") + 1])
    if "--channel" in args:
        channel = args[args.index("--channel") + 1]
        if channel not in ("organic", "paid"):
            print(f"--channel must be 'organic' or 'paid', got {channel!r}")
            return

    config = _load_config()
    print(f"Ghost Typer reels pipeline - {n} video(s), channel={channel}, publish_live={config.get('publish_live', False)}\n")

    try:
        results = await produce_batch(n, channel)
        _print_report(results, config.get("publish_live", False))
    finally:
        await brain_client.aclose()
        await publish.aclose()


if __name__ == "__main__":
    asyncio.run(main())
