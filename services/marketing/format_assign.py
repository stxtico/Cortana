"""format_assign - stage 3 of the Ghost Typer reels pipeline (PROMPTS.md
A19): assigns one of the configured formats to a generated script, enforcing
"no format repeats within N posts" in code (store.py's history), then maps
the canonical script shape into that specific format's real prop type -
confirmed against each component's actual type in
Ghosttyper-web/video/src/formats/*.tsx and video/src/data/scripts.ts, not
guessed.

Composition ids here (generated-*) must match exactly what render.py expects
Root.tsx to have registered - see that file's own docstring for the one
additive change this pipeline needs in Ghosttyper-web/video/src/Root.tsx.

PLAN.md's own instruction for an exhausted pool is "generate a new format
component" - real new TSX code, not something this pipeline attempts
unsupervised against a separate, real product's codebase. When every
configured format is within the no-repeat window, assign_format() still
returns the least-recently-used one (never crashes a batch) but the caller
gets pool_exhausted=True on the result, and pipeline.py surfaces that in the
batch report so a human can go add one - see SKILL.md's "Add a new unique
format" section for how.
"""

import random
from dataclasses import dataclass

import tomllib
from pathlib import Path

from services.marketing import store
from services.marketing.brief import Brief

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

_DOC_TYPE_LABELS = {
    "essay": "Essay",
    "cover_letter": "Cover Letter",
    "personal_statement": "Personal Statement",
    "discussion_post": "Discussion Post",
    "cold_email": "Cold Email",
}


@dataclass
class FormatAssignment:
    format_name: str
    composition_id: str
    props: dict
    duration_frames: int
    pool_exhausted: bool


def _map_ghostreel(script: dict, brief: Brief, video_id: str) -> dict:
    # Matches video/src/data/scripts.ts's VideoScript type exactly.
    return {
        "script": {
            "id": video_id,
            "hook": script["hook"],
            "aiText": script["aiText"],
            "beforeScore": script["beforeScore"],
            "humanText": script["humanText"],
            "afterScore": script["afterScore"],
            "kicker": script["kicker"],
        }
    }


def _map_detector_scan(script: dict, brief: Brief, video_id: str) -> dict:
    # DetectorScanProps = {text, beforeScore, afterScore} - no hook/kicker
    # field on this format; those simply don't apply here, not an omission.
    return {
        "text": script["aiText"],
        "beforeScore": script["beforeScore"],
        "afterScore": script["afterScore"],
    }


def _map_split_screen(script: dict, brief: Brief, video_id: str) -> dict:
    # SplitScreenProps = {aiText, humanText, beforeScore, afterScore, kicker}
    return {
        "aiText": script["aiText"],
        "humanText": script["humanText"],
        "beforeScore": script["beforeScore"],
        "afterScore": script["afterScore"],
        "kicker": script["kicker"],
    }


def _map_giant_stat(script: dict, brief: Brief, video_id: str) -> dict:
    # GiantStatProps = {beforeScore, afterScore, topLabel, kicker} - no text
    # sample on this format (kinetic typography only). topLabel is derived
    # from doc_type, code-only, not LLM-generated - deterministic and simple.
    label = _DOC_TYPE_LABELS.get(brief.doc_type, brief.doc_type.replace("_", " ").title())
    return {
        "beforeScore": script["beforeScore"],
        "afterScore": script["afterScore"],
        "topLabel": f"{label} Flagged",
        "kicker": script["kicker"],
    }


def _map_grade_stamp(script: dict, brief: Brief, video_id: str) -> dict:
    # GradeStampProps = {title, lines, kicker} - no text/scores on this
    # format. lines is a plausible page-line-count derived from the
    # generated text's own length (code-only, not invented) so it stays
    # roughly consistent with how much text the video implies exists,
    # clamped to a believable range for a single essay page.
    label = _DOC_TYPE_LABELS.get(brief.doc_type, brief.doc_type.replace("_", " ").title())
    lines = max(6, min(14, len(script["aiText"]) // 12))
    return {
        "title": f"{label} — Draft",
        "lines": lines,
        "kicker": script["kicker"],
    }


# format name -> (composition id in Root.tsx, duration in frames @30fps,
# prop mapper). Durations copied from each format's real *Duration export
# (GhostReel.REEL_DURATION=500, DetectorScan=450, SplitScreen=420,
# GiantStat=360, GradeStamp=420) - render.py's Root.tsx addition uses these
# same numbers, so they're defined once here and read from there, not
# duplicated as a second guess.
FORMAT_REGISTRY = {
    "ghostreel": ("generated-ghostreel", 500, _map_ghostreel),
    "detector-scan": ("generated-detector-scan", 450, _map_detector_scan),
    "split-screen": ("generated-split-screen", 420, _map_split_screen),
    "giant-stat": ("generated-giant-stat", 360, _map_giant_stat),
    "grade-stamp": ("generated-grade-stamp", 420, _map_grade_stamp),
}


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f).get("marketing", {})


def assign_format(script: dict, brief: Brief, video_id: str) -> FormatAssignment:
    config = _load_config()
    configured = config.get("formats", list(FORMAT_REGISTRY))
    window = config.get("format_no_repeat_window", 3)

    recent = set(store.recent_formats(window))
    available = [f for f in configured if f not in recent]
    pool_exhausted = not available
    if pool_exhausted:
        # Every configured format was used within the window - fall back to
        # the least-recently-used one rather than crashing the batch, but
        # flag it so pipeline.py's report tells a human to add a new format.
        recent_list = store.recent_formats(len(configured))
        available = sorted(configured, key=lambda f: recent_list.index(f) if f in recent_list else -1)[:1]

    chosen = random.choice(available)
    composition_id, duration_frames, mapper = FORMAT_REGISTRY[chosen]
    props = mapper(script, brief, video_id)

    store.record_format(chosen)
    return FormatAssignment(
        format_name=chosen,
        composition_id=composition_id,
        props=props,
        duration_frames=duration_frames,
        pool_exhausted=pool_exhausted,
    )
