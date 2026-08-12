"""brief - stage 1 of the Ghost Typer reels pipeline (PROMPTS.md A19):
rotates angle x doc_type x audience, enforcing variety in code (never left to
the model to self-limit - CLAUDE.md rule 4's whole premise) and restricting
the angle pool by channel per explicit ad-platform-policy instruction: Meta
and TikTok both restrict academic-dishonesty framing, so the
student-caught-cheating angle (detector_panic, paid_safe=false in
cortana.toml) is excluded from paid entirely and reserved for organic.
"""

import random
import tomllib
from dataclasses import dataclass
from pathlib import Path

from services.marketing import store

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"


@dataclass
class Brief:
    angle: str
    doc_type: str
    audience: str
    channel: str  # "paid" or "organic"


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f).get("marketing", {})


def _angle_pool(channel: str, config: dict) -> list[str]:
    angles = config.get("angles", [])
    pool = [a["name"] for a in angles if channel != "paid" or a.get("paid_safe")]
    if not pool:
        raise ValueError(f"No angles configured for channel={channel!r} - check [[marketing.angles]] in cortana.toml.")
    return pool


def generate_brief(channel: str = "organic") -> Brief:
    """Picks the least-recently-used (angle, doc_type, audience) combo from
    the channel-appropriate angle pool - code-enforced variety, matching
    PROMPTS.md's "track what's been used; enforce variety rather than
    hoping for it." Ties (including "never used") broken randomly, not by
    list order, so a fresh store doesn't always start on the same combo."""
    config = _load_config()
    angles = _angle_pool(channel, config)
    doc_types = config.get("doc_types", [])
    audiences = config.get("audiences", [])
    if not doc_types or not audiences:
        raise ValueError("marketing.doc_types and marketing.audiences must both be configured.")

    # Only combos generated under this same channel count as "recent" for
    # this selection - a paid batch shouldn't be blocked from reusing a
    # combo that was only ever used organically, and vice versa.
    recent = [
        (b["angle"], b["doc_type"], b["audience"])
        for b in store.recent_briefs()
        if b["channel"] == channel
    ]
    last_seen = {combo: i for i, combo in enumerate(recent)}  # later index = more recent

    all_combos = [(a, d, u) for a in angles for d in doc_types for u in audiences]
    scored = [(last_seen.get(c, -1), random.random(), c) for c in all_combos]
    scored.sort(key=lambda x: (x[0], x[1]))
    angle, doc_type, audience = scored[0][2]

    store.record_brief(angle, doc_type, audience, channel)
    return Brief(angle=angle, doc_type=doc_type, audience=audience, channel=channel)


def generate_batch(n: int, channel: str = "organic") -> list[Brief]:
    """Sequential, not parallel selection - each call's store.record_brief()
    lands before the next generate_brief() reads history, so variety is
    enforced within one batch too, not just across batches."""
    return [generate_brief(channel) for _ in range(n)]
