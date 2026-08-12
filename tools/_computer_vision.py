"""Vision + coordinates target resolution (PROMPTS.md A18) - the last-resort
tier, only reached when UI Automation, Playwright, and CLI recipes have all
failed to resolve a target. Reuses [models].vision (gemma3:12b, A14's already-
validated choice) via services/brain/client.py's stream(model=..., format=...),
the exact message-shape precedent tools/cad.py's _vision_check() already
established: {"role": "user", "content": prompt, "images": [base64...]}.

Every click resolved through this module requires confirmation regardless of
what the action does (tools/computer.py's execute() enforces this, not this
module) - a deliberate decision made explicit with the user during this
build, not the module-level default reasoning that gates the four named
action categories (send/delete/purchase/submit). The reason is specific to
this tier: A14's CLAUDE.md entry found this exact model fabricates specific
wrong claims about an image rather than admitting uncertainty when asked to
critique, and separately produced a real false negative even in an already-
hardened setup. A "harmless" vision-resolved click isn't harmless here
because the *target itself* is unreliably identified, not because the action
is risky - the same reasoning A14 used to make geometric validation, not
vision, the ground truth for CAD. There's no equivalent geometric ground
truth for an arbitrary GUI click, so the confirmation prompt is what fills
that role instead: it must state plainly that the target was vision-resolved
and describe what the model believes it's about to click, so a
misidentification can be caught before it fires, not after.
"""

import base64
import io
import json

from PIL import ImageGrab

from services.brain import client as brain_client

_PROMPT = """You are looking at a full desktop screenshot. Find this on-screen target:
{description}

Respond with ONLY a JSON object: {{"found": true/false, "x": <int>, "y": <int>, "what_you_see": "<one sentence describing exactly what's at that point, so a human can catch a misidentification before anything is clicked>"}}
x/y are pixel coordinates in this exact screenshot's own resolution. If you cannot confidently locate the target, respond {{"found": false, "x": 0, "y": 0, "what_you_see": "<why not>"}}."""


def _capture_screenshot_b64() -> tuple[str, tuple[int, int]]:
    """Real full-virtual-desktop screenshot (all monitors), PNG-encoded,
    base64 - same encode-to-base64-for-Ollama's images field pattern
    tools/cad.py's _vision_check() already uses for render comparisons.
    Returns the image alongside its own pixel size, since the model is asked
    to answer in THIS image's coordinate space - the caller is responsible
    for treating (x, y) as coordinates within this exact screenshot, not
    assuming any particular screen resolution."""
    img = ImageGrab.grab(all_screens=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii"), img.size


async def resolve(vision_model: str, description: str) -> tuple[int, int, str] | None:
    """Returns (x, y, what_you_see) in real desktop pixel coordinates if the
    model claims to have found the target, or None if it couldn't (an
    explicit abstention path, not a guess forced through - same "cannot
    tell" option A14's CAD vision check gives, for the same reason: a model
    that fabricates specifics rather than admitting uncertainty needs an
    honest way out, not just a prompt asking it to be honest)."""
    image_b64, _size = _capture_screenshot_b64()
    messages = [{"role": "user", "content": _PROMPT.format(description=description), "images": [image_b64]}]
    raw = ""
    async for token in brain_client.stream(messages, model=vision_model, format="json"):
        raw += token
    try:
        verdict = json.loads(raw)
    except json.JSONDecodeError:
        return None  # unparseable vision output is a miss, not a crash - same tolerance tools/cad.py's vision check applies
    if not verdict.get("found"):
        return None
    x, y = verdict.get("x"), verdict.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    return round(x), round(y), str(verdict.get("what_you_see", ""))
