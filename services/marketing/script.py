"""script - stage 2 of the Ghost Typer reels pipeline (PROMPTS.md A19):
generates the actual before/after text pair for one brief, against the craft
rules in Ghosttyper-web/.claude/skills/ghost-typer-reels/SKILL.md (quoted
below, not paraphrased, since the exact tell-words and score ranges are the
whole craft). Reuses services/brain/client.py's stream() - same pattern
tools/cad.py's vision check already established for a structured-JSON call
against a specific model.
"""

import json

from services.brain import client as brain_client
from services.marketing.brief import Brief

# Real, sympathetic-but-distinct framings, not bare enum strings - the model
# needs to know what each angle actually means to write toward it. Must stay
# in sync with [[marketing.angles]] in cortana.toml.
_ANGLE_CONTEXT = {
    "false_positive": "the writer's own genuinely human writing got wrongly flagged as AI - a detector malfunction, not anything the writer did wrong.",
    "non_native_writer": "the writer is a non-native English speaker whose natural phrasing patterns get mistaken for AI - sympathetic and true, detectors really do misfire on this.",
    "cheap_alternative": "a cheaper or free humanizer tool got the score partway down but not far enough - Ghost Typer finishes the job a lesser tool started.",
    "deadline_panic": "the work is due very soon and just got flagged unexpectedly - time pressure, not an accusation.",
    "detector_panic": "the writer got directly caught and flagged by a detector before submitting - the classic caught-red-handed fear.",
}

_DOC_TYPE_CONTEXT = {
    "essay": "a class essay",
    "cover_letter": "a job cover letter",
    "personal_statement": "a college application personal statement",
    "discussion_post": "a class discussion board post",
    "cold_email": "a cold outreach email",
}

_AUDIENCE_CONTEXT = {
    "student": "a student",
    "professional": "a working professional",
    "non_native_speaker": "a non-native English speaker",
}

# Quoted from the ghost-typer-reels skill, not paraphrased - the tell-words
# and score ranges are the actual craft, not incidental detail.
_SYSTEM_PROMPT = """You write short before/after text pairs for Ghost Typer marketing videos. \
Ghost Typer is a tool that rewrites AI-sounding text so it reads as human and passes AI \
detectors. The emotional beat is relief.

Output ONLY a JSON object with these exact fields, nothing else:
{
  "hook": "opening line, short and concrete",
  "aiText": "the deliberately AI-sounding 'before' text",
  "beforeScore": <int 88-99>,
  "humanText": "the humanized 'after' text",
  "afterScore": <int 5-14>,
  "kicker": "closing line above the logo"
}

Rules:
- aiText: load it with real AI-detector tells - phrases like "In today's world," \
"moreover," "furthermore," "it is important to note," "plays a crucial role," \
"multifaceted," "delve into," "underscores," "aforementioned." It should sound \
exactly like the kind of writing a detector flags.
- humanText: the opposite of aiText - first person, contractions, one specific \
concrete detail, varied sentence rhythm (one short sentence among longer ones). \
Same underlying idea as aiText, roughly the same length, so it reads as a rewrite \
of the same content, not a different topic.
- beforeScore: an integer 88-99 (never 0 or 100 - reads as fake).
- afterScore: an integer 5-14 (never 0 or 100).
- hook: concrete and a little alarming, not abstract. "Turnitin said 91% AI. It \
wasn't." beats "AI detection is a problem."
- kicker: one short closing line, no period-less slogans, fits above a logo.

Return raw JSON only - no markdown fences, no commentary."""


def _user_prompt(brief: Brief) -> str:
    angle = _ANGLE_CONTEXT.get(brief.angle, brief.angle)
    doc_type = _DOC_TYPE_CONTEXT.get(brief.doc_type, brief.doc_type)
    audience = _AUDIENCE_CONTEXT.get(brief.audience, brief.audience)
    return (
        f"Write one before/after pair for {doc_type}, written by {audience}. "
        f"The angle: {angle}"
    )


async def generate_script(brief: Brief) -> dict:
    """Returns {hook, aiText, beforeScore, humanText, afterScore, kicker}.
    think=False, not a config toggle - a straightforward single-turn creative
    JSON task, no tool chain to reason through (unlike agent.py's
    think=True default, which A8 found necessary specifically for multi-step
    tool-call reliability). Same reasoning tools/cad.py's _vision_check()
    already documents for its own single-purpose JSON call."""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _user_prompt(brief)},
    ]
    chunks = []
    async for token in brain_client.stream(messages, think=False, format="json"):
        chunks.append(token)
    raw = "".join(chunks).strip()
    data = json.loads(raw)  # let a malformed response raise - pipeline.py's caller decides how to handle a failed script

    required = {"hook", "aiText", "beforeScore", "humanText", "afterScore", "kicker"}
    missing = required - data.keys()
    if missing:
        raise ValueError(f"Generated script missing fields: {missing} (got {data!r})")
    return data
