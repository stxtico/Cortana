"""Vision + coordinates target resolution (PROMPTS.md A18/A22) - the
last-resort tier, only reached when UI Automation, Playwright, and CLI
recipes have all failed to resolve a target. Uses [models].vision_grounding
(gta1-7b as of A22, replacing gemma3:12b - a deliberately separate config key
from [models].vision, which tools/cad.py's description/comparison checks
still use; see config/cortana.toml's own comment for why the two shouldn't
share one key) via services/brain/client.py's stream(model=..., format=...),
the exact message-shape precedent tools/cad.py's _vision_check() already
established: {"role": "user", "content": prompt, "images": [base64...]}.

Every click resolved through this module requires confirmation regardless of
what the action does (tools/computer.py's execute() enforces this, not this
module) - a deliberate decision made explicit with the user during this
build, not the module-level default reasoning that gates the four named
action categories (send/delete/purchase/submit). A14's original reasoning
here was that gemma3:12b (a general-purpose VLM) fabricates specific wrong
claims about an image rather than admitting uncertainty - A22 found that was
too broad a conclusion (general-purpose VLMs are unreliable at GUI grounding,
but purpose-built grounding models are a different class of thing: GTA1-7B
measured 81.8% on a real 33-target benchmark built from this machine's own
apps, not gemma3:12b's failure pattern - see docs/history/A22.md). The
confirmation requirement stays regardless: 81.8% (68.4% on hard/icon-only
targets specifically) still isn't accurate enough to act on unconfirmed, so
a "harmless" vision-resolved click still isn't harmless here, just for a
different, better-quantified reason than before - the same reasoning A14
used to make geometric validation, not vision, the ground truth for CAD.
There's no equivalent geometric ground truth for an arbitrary GUI click, so
the confirmation prompt is what fills that role instead: it must state
plainly that the target was vision-resolved and describe what the model
believes it's about to click, so a
misidentification can be caught before it fires, not after.
"""

import base64
import io
import re

import win32gui
from PIL import ImageGrab

from services.brain import client as brain_client

# GTA1-7B's own documented prompt format (its Hugging Face model card),
# exactly what docs/history/A22.md's 33-target benchmark used - not the old
# "found: true/false" JSON schema this module asked for before A22, which
# was designed around gemma3:12b and was never actually validated against
# GTA1-7B. Sending GTA1 a prompt shape it wasn't benchmarked against would
# mean the 81.8% number doesn't transfer to what production actually does -
# matching the exact validated format is the point, not an arbitrary choice.
_GROUNDING_SYSTEM_PROMPT = """You are an expert UI element locator. Given a GUI image and a user's element description, provide the coordinates of the specified element as a single (x,y) point. The image resolution is height {h} and width {w}. For elements with area, return the center point.

Output the coordinate pair exactly:
(x,y)"""

# GTA1-7B's own trained output is just "(x,y)" - no description, no
# abstention signal (confirmed empirically during the A22 benchmark: it
# always emitted some coordinate, never "not found"). The confirmation
# prompt this tier feeds into (tools/computer.py's execute()) needs a real
# human-facing description of what's actually at that point so a
# misidentification can be caught before it fires - a second, small call to
# [models].vision (gemma3:12b, already validated for exactly this kind of
# open-ended description task, A14) on a tight crop around the coordinate
# gets that, reusing each model for what it was actually shown to be good
# at rather than asking GTA1 to do a job outside its trained format.
_DESCRIBE_PROMPT = (
    "What UI element is at the center of this cropped screenshot? Answer in one "
    "short sentence describing exactly what you see there."
)
_DESCRIBE_CROP_RADIUS = 100


def _capture_screenshot_b64(hwnd: int | None) -> tuple[str, tuple[int, int], tuple[int, int, int, int] | None]:
    """Real screenshot, PNG-encoded, base64 - same encode-to-base64-for-Ollama's
    images field pattern tools/cad.py's _vision_check() already uses for
    render comparisons. Crops to hwnd's real window bounds when given (A22 -
    the grounder was benchmarked on per-app crops, not the full multi-monitor
    desktop; sending the full desktop here would have meant production
    accuracy never actually matched the 81.8% measured in docs/history/A22.md,
    since the target is a much smaller fraction of a much busier image).
    Falls back to the full virtual desktop when hwnd is unavailable (e.g. the
    resolution tier reaching vision without a known window, matching the
    original full-desktop behavior rather than failing outright).

    Returns the image, its own pixel size (what the model is told and asked
    to answer within), and the window's real screen-absolute rect if cropped
    (None if not) - resolve() below uses that rect to convert the model's
    crop-relative answer back to real desktop-absolute coordinates before
    ever handing it to a caller, so this crop is invisible to
    tools/computer.py's contract: resolve() always returns desktop-absolute
    (x, y), regardless of how it captured the image internally."""
    if hwnd is not None:
        rect = win32gui.GetWindowRect(hwnd)
        if rect[2] > rect[0] and rect[3] > rect[1]:
            img = ImageGrab.grab(bbox=rect)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("ascii"), img.size, rect
    img = ImageGrab.grab(all_screens=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii"), img.size, None


async def _describe_point(description_model: str, full_image_b64_size: tuple[int, int], hwnd: int | None, x: int, y: int) -> str:
    """Stage 2 (A22): a tight crop around GTA1's chosen point, described by
    [models].vision (gemma3:12b) - the human-facing "what_you_see" text the
    confirmation prompt needs. Re-captures rather than reusing stage 1's
    image/coordinates directly, since x/y here are already desktop-absolute
    (resolve() converts before calling this) and a fresh crop keeps the
    logic simple (real screen coordinates in, real screen coordinates out)
    rather than threading crop-relative math through two different call
    sites. Best-effort: an empty string on any failure here still lets the
    caller show the coordinate, just without the descriptive safety text -
    better than losing the whole resolution over a second-stage hiccup."""
    try:
        radius = _DESCRIBE_CROP_RADIUS
        img = ImageGrab.grab(bbox=(x - radius, y - radius, x + radius, y + radius))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        crop_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        messages = [{"role": "user", "content": _DESCRIBE_PROMPT, "images": [crop_b64]}]
        raw = ""
        async for token in brain_client.stream(messages, model=description_model):
            raw += token
        return raw.strip()
    except Exception:
        return ""


async def resolve(grounding_model: str, description_model: str, description: str, hwnd: int | None = None) -> tuple[int, int, str] | None:
    """Returns (x, y, what_you_see) in real desktop pixel coordinates, or
    None if grounding_model's output couldn't be parsed into a coordinate
    (GTA1-7B has no native "not found" signal - confirmed empirically during
    the A22 benchmark, it always emits some (x,y) - so a miss here means
    unparseable output, not a self-reported abstention; Step 3's post-action
    verification and the mandatory confirmation gate below are what actually
    catch a wrong-but-confident answer, not this tier admitting uncertainty).

    hwnd (A22): the target app's real window handle, when known - crops the
    capture to that window (see _capture_screenshot_b64()'s docstring for
    why that matters for accuracy) and converts grounding_model's
    crop-relative answer back to desktop-absolute before returning, so
    callers never need to know a crop happened."""
    image_b64, (w, h), rect = _capture_screenshot_b64(hwnd)
    messages = [
        {"role": "system", "content": _GROUNDING_SYSTEM_PROMPT.format(h=h, w=w)},
        {"role": "user", "content": f"Find: {description}", "images": [image_b64]},
    ]
    raw = ""
    async for token in brain_client.stream(messages, model=grounding_model):
        raw += token
    m = re.search(r"\(?\s*(-?\d+)\s*,\s*(-?\d+)\s*\)?", raw)
    if not m:
        return None  # unparseable grounding output is a miss, not a crash - same tolerance tools/cad.py's vision check applies
    x, y = int(m.group(1)), int(m.group(2))
    if rect is not None:
        x, y = x + rect[0], y + rect[1]
    what_you_see = await _describe_point(description_model, (w, h), hwnd, x, y)
    return x, y, what_you_see
