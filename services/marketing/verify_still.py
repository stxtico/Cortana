"""verify_still - stage 5 of the Ghost Typer reels pipeline (PROMPTS.md
A19): automated still verification. Renders (via render.py) the payoff
frame and the longest-text frame, feeds both to [models].vision, and asks
specifically for the three failure modes PROMPTS.md and the ghost-typer-reels
skill both call out: text overflow, a dark/inverted logo, and off-centre
numbers.

Same message shape as tools/cad.py's _vision_check() (A14) - one user
message, one prompt, both images base64-encoded in the "images" list, JSON
mode. Explicit instruction for this stage: vision is the weak tier here too
(A14/A18 both measured it fabricating specifics) - a false flag just costs a
re-check, so its output is a SIGNAL to look, never a verdict. This function
never returns pass/fail on its own; it returns what the model claimed, and
pipeline.py logs that alongside what actually shipped so a human (or a
later, better vision model) can judge whether the claim was real.
"""

import base64
import json
from pathlib import Path

import tomllib

from services.brain import client as brain_client

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

_VISION_PROMPT = """You are checking two still frames from a vertical (1080x1920) marketing \
video before it ships. Frame 1 is the frame with the most on-screen text. Frame 2 is the \
"payoff" frame (the moment the video's point lands - a score, a verdict, a stamp).

Look specifically for these three problems, and ONLY these:
1. text_overflow: does any text run off the edge of its card/container, or get cut off?
2. dark_logo: is the Ghost Typer ghost logo (should be a bright white/light ghost shape) \
showing as dark, black, or inverted instead?
3. offcentre_numbers: is any large number (a percentage, a score) visibly off-center or \
misaligned within its frame?

Respond with ONLY this JSON, nothing else:
{
  "text_overflow": true/false,
  "dark_logo": true/false,
  "offcentre_numbers": true/false,
  "notes": "one sentence describing what you actually saw, or empty if nothing notable"
}

If you are not confident about a problem, say false - do not guess a problem into existence."""


def _load_vision_model() -> str:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config["models"]["vision"]


async def check_stills(longest_text_path: Path, payoff_path: Path) -> dict:
    """Returns the raw model claim (never raises on a flagged issue - that's
    the point). {"text_overflow", "dark_logo", "offcentre_numbers", "notes",
    "parsed_ok"} - parsed_ok=False means the model's output wasn't valid
    JSON, which is treated the same as tools/cad.py's own precedent
    (unparseable vision output is discarded, not treated as a failure - weak
    signal only)."""
    images_b64 = [
        base64.b64encode(longest_text_path.read_bytes()).decode("ascii"),
        base64.b64encode(payoff_path.read_bytes()).decode("ascii"),
    ]
    messages = [{"role": "user", "content": _VISION_PROMPT, "images": images_b64}]

    vision_model = _load_vision_model()
    chunks = []
    async for token in brain_client.stream(messages, model=vision_model, format="json"):
        chunks.append(token)
    raw = "".join(chunks).strip()

    try:
        verdict = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "text_overflow": False, "dark_logo": False, "offcentre_numbers": False,
            "notes": "", "parsed_ok": False, "raw": raw,
        }

    return {
        "text_overflow": bool(verdict.get("text_overflow")),
        "dark_logo": bool(verdict.get("dark_logo")),
        "offcentre_numbers": bool(verdict.get("offcentre_numbers")),
        "notes": verdict.get("notes", "") or "",
        "parsed_ok": True,
        "raw": raw,
    }


def flagged(verdict: dict) -> bool:
    """Whether ANY of the three checks came back true - used by pipeline.py
    to decide whether to surface this video for a closer human look, never
    to auto-reject it (signal, not verdict)."""
    return verdict["text_overflow"] or verdict["dark_logo"] or verdict["offcentre_numbers"]
