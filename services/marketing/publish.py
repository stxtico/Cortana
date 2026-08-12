"""publish - stage 7 of the Ghost Typer reels pipeline (PROMPTS.md A19):
uploads the verified MP4 to Supabase Storage (content_posts.media_url must
be a real public URL - Meta fetches the video, it cannot accept uploaded
bytes, confirmed against the real migration
Ghosttyper-web/supabase/migrations/20260719000100_instagram_content_pipeline.sql),
builds a per-video UTM-tagged link, and queues a row in content_posts.

Real Supabase writes are gated behind [marketing].publish_live (default
false) - explicit instruction: the insert path is built for real, not
stubbed, but stays off until a reviewed dry-run batch's rows have actually
been read. Storage upload happens for real even in dry-run (explicit
instruction: the printed row's media_url should be a real, clickable URL) -
under [marketing].dry_run_storage_prefix so those objects are visibly
distinguishable from real approved-content uploads. Only the content_posts
INSERT itself is gated; uploading a video to a bucket that already defaults
to private-until-approved has no downstream effect on its own (nothing
reads it unless a content_posts row points at it and that row is later
approved) - the insert is the consequential write, not the upload.

status is never set to anything but the table's own default ('draft') -
approval at /admin/content stays the only path forward, no bypass, matching
CLAUDE.md rule 4 and A9's whole dispatcher-gate precedent.

Reuses services/brain/client.py's persistent-client pattern (CLAUDE.md
rule 7) rather than a new httpx.AsyncClient per call.
"""

import os
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv

from services.marketing.brief import Brief

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

_client: httpx.AsyncClient | None = None


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f).get("marketing", {})


def is_configured() -> bool:
    """Read-only, no side effects (CLAUDE.md rule 10) - just checks whether
    the two env vars this module needs are set. Same names Ghosttyper-web's
    own .env.example uses for SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY, so
    pointing cortana's own .env at the same real values (a second place
    configuring the same secret, not a shared file - CLAUDE.md rule 9, "no
    secrets in the repo") needs no name translation."""
    load_dotenv()
    return bool(os.environ.get("SUPABASE_URL")) and bool(os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        load_dotenv()
        url = os.environ["SUPABASE_URL"].rstrip("/")
        key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        _client = httpx.AsyncClient(
            base_url=url,
            headers={"Authorization": f"Bearer {key}", "apikey": key},
            timeout=60.0,
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def bucket_exists(bucket: str) -> bool:
    client = _get_client()
    resp = await client.get(f"/storage/v1/bucket/{bucket}")
    return resp.status_code == 200


async def upload_video(mp4_path: Path, video_id: str, dry_run: bool) -> str:
    """Uploads the real file, returns the real public URL - happens even in
    dry-run (explicit instruction) so the printed row's media_url is
    genuinely clickable, not a placeholder. Raises on any real failure
    (bucket missing, upload rejected) rather than returning a fake URL -
    pipeline.py's caller needs to know this failed, not silently proceed."""
    config = _load_config()
    bucket = config["storage_bucket"]
    if not await bucket_exists(bucket):
        raise RuntimeError(
            f"Storage bucket {bucket!r} doesn't exist in this Supabase project - "
            "create it (public) via the Supabase dashboard before publishing. "
            "Not auto-created (CLAUDE.md rule 10 - a check/setup step must not "
            "silently create production infrastructure)."
        )

    prefix = config.get("dry_run_storage_prefix", "dry-run") if dry_run else None
    object_path = f"{prefix}/{video_id}.mp4" if prefix else f"{video_id}.mp4"

    client = _get_client()
    resp = await client.post(
        f"/storage/v1/object/{bucket}/{object_path}",
        content=mp4_path.read_bytes(),
        headers={"Content-Type": "video/mp4"},
    )
    resp.raise_for_status()

    base_url = os.environ["SUPABASE_URL"].rstrip("/")
    return f"{base_url}/storage/v1/object/public/{bucket}/{object_path}"


def build_utm_link(brief: Brief, video_id: str) -> str:
    config = _load_config()
    params = {
        "utm_source": "instagram",
        "utm_medium": brief.channel,
        "utm_campaign": brief.angle,
        "utm_content": video_id,
    }
    return f"{config['landing_url']}?{urllib.parse.urlencode(params)}"


def build_row(brief: Brief, script: dict, format_name: str, video_id: str, media_url: str) -> dict:
    """content_posts row, matching the real schema (status omitted - the
    table's own default 'draft' applies, never overridden here). Caption
    carries no raw URL - Instagram captions don't render clickable links
    (confirmed via SKILL.md's own documented understanding), so the UTM
    link lives in metadata.utm_link for A20's later attribution wiring, not
    in the caption text."""
    utm_link = build_utm_link(brief, video_id)
    caption = f"{script['hook']} {script['kicker']}".strip()
    return {
        "kind": "reel",
        "caption": caption,
        "media_url": media_url,
        "metadata": {
            "video_id": video_id,
            "angle": brief.angle,
            "doc_type": brief.doc_type,
            "audience": brief.audience,
            "channel": brief.channel,
            "format": format_name,
            "hook": script["hook"],  # A20 - stored as its own field so the attribution report can rank by hook, not just parse it back out of caption
            "utm_link": utm_link,
        },
    }


@dataclass
class PublishResult:
    row: dict
    inserted: bool  # False in dry-run - row was printed/returned, not written
    content_posts_id: int | None


async def queue_post(row: dict) -> PublishResult:
    """Real insert, only ever called when [marketing].publish_live is true -
    pipeline.py is the one place that checks that flag, so this function
    itself has no dry-run branch; it either inserts for real or isn't
    called at all."""
    client = _get_client()
    resp = await client.post(
        "/rest/v1/content_posts",
        json=row,
        headers={"Content-Type": "application/json", "Prefer": "return=representation"},
    )
    resp.raise_for_status()
    inserted = resp.json()[0]
    return PublishResult(row=inserted, inserted=True, content_posts_id=inserted.get("id"))
