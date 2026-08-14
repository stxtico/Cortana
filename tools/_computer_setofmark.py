"""Set-of-mark disambiguation (PROMPTS.md A22 Step 2) - reached only when
tools/_computer_uia.py's exact-name resolve() has missed but find_candidates()
found one or more loose matches for the same target description. Screenshots
the target window, draws a numbered box over each candidate's real UIA
rectangle, and asks a vision model to pick a number - removes coordinate
hallucination as a failure mode entirely (PROMPTS.md A22 Step 2's own
framing): the model can pick the wrong box, but every box is a real,
UIA-sourced rectangle, so it can never invent a location that doesn't exist.

Deliberately uses [models].vision (gemma3:12b), not [models].vision_grounding
(GTA1-7B) - this is a multiple-choice question ("which of these N labeled
regions matches this description"), the general-reasoning task A14/A22 already
validated gemma3:12b for, not the raw-coordinate-grounding task GTA1-7B was
RL-trained for. Reusing each model for the job it was actually shown to be
good at, same reasoning A22's Step 1 swap applied the other way around.

Deliberately narrow scope (this session's explicit instruction, not
PROMPTS.md's original "cross-validate UIA against vision on every action"
framing): reached only when tools/computer.py's resolution path finds UIA's
own match ambiguous going in (find_candidates() returned something after an
exact-name miss), never run alongside a clean UIA hit. A22 Step 1's own
overlap analysis found 25/33 benchmark targets had both UIA and the grounder
firing, and of those, 21 were the grounder merely agreeing with an already-
exact answer and the other 4 were the grounder being wrong - an always-
run-both design adds latency on every action and occasionally overrules a
correct answer with a worse one, for no measured benefit.
"""

import base64
import io

import win32gui
from PIL import Image, ImageDraw, ImageFont, ImageGrab

from services.brain import client as brain_client
from tools._computer_uia import ResolvedElement

_PROMPT = """You are looking at a screenshot with numbered boxes drawn over candidate UI elements. The user wants: {target!r}

Candidates:
{listing}

Reply with ONLY the number of the box that best matches the user's description. If none of them plausibly match, reply exactly: none"""

_BOX_COLOR = (255, 0, 0)
_LABEL_FG = (255, 255, 255)


def _draw_marks(img: Image.Image, candidates: list[ResolvedElement], origin: tuple[int, int]) -> Image.Image:
    """Draws a numbered box over each candidate's real rect, converted from
    desktop-absolute (what UIA reports) to crop-relative (what the screenshot
    actually shows) via origin, the crop's own top-left corner - the same
    absolute-to-relative conversion tools/_computer_vision.py's resolve()
    already does the other direction (crop-relative model answer back to
    desktop-absolute)."""
    marked = img.convert("RGB").copy()
    draw = ImageDraw.Draw(marked)
    font = ImageFont.load_default()
    ox, oy = origin
    for i, c in enumerate(candidates, start=1):
        left, top, right, bottom = c.left - ox, c.top - oy, c.right - ox, c.bottom - oy
        draw.rectangle([left, top, right, bottom], outline=_BOX_COLOR, width=2)
        label = str(i)
        text_bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = text_bbox[2] - text_bbox[0], text_bbox[3] - text_bbox[1]
        label_top = max(0, top - th - 4)
        draw.rectangle([left, label_top, left + tw + 4, label_top + th + 4], fill=_BOX_COLOR)
        draw.text((left + 2, label_top + 2), label, fill=_LABEL_FG, font=font)
    return marked


async def resolve(
    description_model: str,
    target: str,
    candidates: list[ResolvedElement],
    hwnd: int | None = None,
) -> ResolvedElement | None:
    """Returns the candidate the model picked, or None if it picked "none" or
    its answer couldn't be parsed into a valid index - both treated as a
    miss, same "a resolution miss is an ordinary outcome" tolerance every
    other resolution tier in this file uses (callers fall through further,
    never treat this as an error). Candidates outside hwnd's own window are
    dropped from the marked set before drawing - the caller only screenshots
    one window at a time, so a candidate the model can't actually see in the
    image it was given can't meaningfully be picked."""
    if hwnd is not None:
        candidates = [c for c in candidates if c.hwnd == hwnd]
    if not candidates:
        return None

    if hwnd is not None:
        rect = win32gui.GetWindowRect(hwnd)
        img = ImageGrab.grab(bbox=rect)
        origin = (rect[0], rect[1])
    else:
        img = ImageGrab.grab(all_screens=True)
        origin = (0, 0)

    marked = _draw_marks(img, candidates, origin)
    buf = io.BytesIO()
    marked.save(buf, format="PNG")
    marked_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    listing = "\n".join(f"{i}. {c.name!r} ({c.control_type})" for i, c in enumerate(candidates, start=1))
    messages = [{"role": "user", "content": _PROMPT.format(target=target, listing=listing), "images": [marked_b64]}]
    raw = ""
    async for token in brain_client.stream(messages, model=description_model):
        raw += token
    raw = raw.strip().lower()
    if raw.startswith("none"):
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    idx = int(digits)
    if not (1 <= idx <= len(candidates)):
        return None
    return candidates[idx - 1]
