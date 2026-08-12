"""report - stage 9 of the Ghost Typer reels pipeline (PROMPTS.md A20):
ranks hooks, formats, angles, and document types by conversions per
thousand views. Every number is either real or explicitly marked
unmeasurable - never estimated or invented to fill a gap in the real
schema (see attribution.py's docstring for exactly what's missing today).

"Views" has no real data source at all yet (Instagram Insights are never
fetched anywhere in Ghosttyper-web - see attribution.py). Rather than
fabricate a views count, this reads an optional content_posts.metadata.views
if some future mechanism ever populates it, and marks a ranking
unmeasurable whenever it's absent - the rate is never computed against a
made-up denominator.
"""

from collections import defaultdict
from dataclasses import dataclass

from services.marketing import attribution


@dataclass
class Ranking:
    key: str
    videos: int
    signups: int
    conversions: int
    views: int | None
    conversions_per_1000_views: float | None
    measurable: bool


def _dimension_value(metadata: dict, dimension: str) -> str:
    return metadata.get(dimension) or "(unknown)"


async def build_report(dimension: str) -> list[Ranking]:
    """dimension is one of 'hook', 'format', 'angle', 'doc_type' - the
    fields A19's publish.py stores in content_posts.metadata."""
    rows = await attribution.fetch_content_rows()
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = _dimension_value(row.get("metadata") or {}, dimension)
        groups[key].append(row)

    rankings = []
    for key, group_rows in groups.items():
        total_signups = 0
        total_conversions = 0
        total_views = 0
        any_conversions_measurable = False
        any_views = False
        for row in group_rows:
            video_id = (row.get("metadata") or {}).get("video_id")
            conv = await attribution.fetch_conversions_for(video_id) if video_id else {"signups": 0, "conversions": 0, "measurable": False}
            total_signups += conv["signups"]
            total_conversions += conv["conversions"]
            any_conversions_measurable = any_conversions_measurable or conv["measurable"]

            views = (row.get("metadata") or {}).get("views")
            if isinstance(views, (int, float)):
                total_views += views
                any_views = True

        rate = (total_conversions / total_views * 1000) if (any_views and total_views > 0) else None
        rankings.append(Ranking(
            key=key,
            videos=len(group_rows),
            signups=total_signups,
            conversions=total_conversions,
            views=total_views if any_views else None,
            conversions_per_1000_views=rate,
            measurable=any_conversions_measurable and any_views,
        ))

    rankings.sort(key=lambda r: (r.conversions_per_1000_views is None, -(r.conversions_per_1000_views or 0)))
    return rankings


def format_report(dimension: str, rankings: list[Ranking]) -> str:
    lines = [f"=== Ranking by {dimension} (conversions per 1000 views) ==="]
    if not rankings:
        lines.append("(no content_posts rows yet - publish_live is false and/or no batch has published)")
        return "\n".join(lines)

    for r in rankings:
        rate_str = f"{r.conversions_per_1000_views:.1f}" if r.conversions_per_1000_views is not None else "unmeasurable"
        views_str = str(r.views) if r.views is not None else "?"
        lines.append(f"{r.key!r}: {r.videos} video(s), {r.signups} signups, {r.conversions} conversions, {views_str} views -> {rate_str}")

    if not any(r.measurable for r in rankings):
        lines.append("")
        lines.append("No ranking above reflects real conversions or views yet - see attribution.py's")
        lines.append("UTM_ATTRIBUTIONS_SCHEMA for what's structurally missing in Ghosttyper-web before")
        lines.append("this report can produce real numbers.")
    return "\n".join(lines)
