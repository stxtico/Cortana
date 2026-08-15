"""look_at_screen (PROMPTS.md A22 Step 4) - the read-only counterpart to
tools/computer.py: she can answer questions about what's on screen without
acting on it. Same tiering discipline as computer.py's resolution path -
prefer the accessibility tree for anything structured (window titles,
control names, visible text are exact via UIA; asking a vision model to
read them is strictly worse), vision handles what UIA can't express (layout,
images, "what does this look like"). Both signals are gathered every call
(this is an on-request tool, not a per-turn cost) and returned with their
attribution baked directly into the string, not left to the calling model to
get right - the same "constrain the shape, don't trust the wording" reasoning
CLAUDE.md's documented ~2/3 instruction-following ceiling makes necessary
everywhere else in this codebase (A5a, A18's find_file). UIA-sourced content
is stated as fact ("reads:"); vision-sourced content is explicitly hedged
("it looks like...") - PROMPTS.md's own instruction, "confidence is
uncorrelated with accuracy here."

No REQUIRES_CONFIRMATION - a pure read, same as read_file/list_dir/
fetch_url/web_search (CLAUDE.md rule 4: confirmation gates actions that
delete, send, spend, submit, or unlock, not observation).

Privacy rails, all real code here, not persona text:
- Screenshots are captured into memory (PIL Image / BytesIO) and never
  written to disk anywhere in this module - grep for `.save(` here and the
  only call is to an in-memory buffer, same pattern tools/_computer_vision.py
  and tools/_computer_setofmark.py already established.
- [tools.screen].excluded_windows (config/cortana.toml) is checked BEFORE
  any capture happens - a match refuses the whole call outright, not just
  the vision half, so an excluded window's on-screen text never reaches UIA
  extraction either. Empty by default, same "no directory is write-
  authorized until you explicitly add one" bootstrap precedent as
  [tools].write_whitelist_dirs - this project doesn't guess at which
  password manager or banking app a given install actually uses.
- Capture only ever happens inside execute(), called only when the model
  invokes this tool in response to a real request - there is no background
  poller wired to this module, satisfying PROMPTS.md's "capture on request
  or an explicit trigger, never continuously" without needing a separate
  enforcement mechanism.
"""

import base64
import io
import tomllib
from pathlib import Path

import pywinauto
import win32gui
from PIL import ImageGrab

from services.brain import client as brain_client
from tools import _computer_uia

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

_VISION_PROMPT = (
    "Answer this question about what's visible in the screenshot, in one or two sentences: {question}\n"
    "Describe only what you can actually see - don't guess at text you can't clearly read."
)


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "look_at_screen",
            "description": (
                "Look at what's currently on screen and answer a question about it, without "
                "clicking or typing anything - read-only. Combines exact on-screen text (from "
                "the accessibility tree, where available) with a vision model's description of "
                "layout and visuals. Defaults to whatever window currently has focus; pass `app` "
                "to look at a specific application by process name instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "What to answer about what's on screen, e.g. 'what does this error say' or 'what does this look like'."},
                    "app": {"type": "string", "description": "Optional: an executable name substring (e.g. 'chrome', 'explorer') to look at that app's window instead of whatever currently has focus."},
                },
                "required": ["question"],
            },
        },
    }


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def _screen_config() -> dict:
    return _load_config().get("tools", {}).get("screen", {})


def _resolve_target_hwnd(app: str | None) -> int | None:
    if app:
        hwnds = _computer_uia.find_top_level_hwnds(app)
        return hwnds[0] if hwnds else None
    hwnd = win32gui.GetForegroundWindow()
    return hwnd if hwnd else None


def _is_excluded(title: str, excluded: list[str]) -> bool:
    title_l = title.lower()
    return any(term.lower() in title_l for term in excluded)


def _gather_uia_text(hwnd: int, max_items: int) -> list[str]:
    """Real accessible names from the window's own UIA tree - exact, cheap,
    no model call. Same _walk() traversal tools/_computer_uia.py's own
    resolve()/find_candidates() already use, reused here for reading rather
    than matching."""
    try:
        app = pywinauto.Application(backend="uia")
        app.connect(handle=hwnd)
        window = app.window(handle=hwnd)
    except Exception:
        return []
    seen = set()
    texts = []
    try:
        elements = _computer_uia._walk(window)
    except Exception:
        return []
    for element in elements:
        if len(texts) >= max_items:
            break
        name = (element.element_info.name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        texts.append(name)
    return texts


def _capture_region_b64(hwnd: int) -> str:
    """In-memory only - PNG bytes base64-encoded for the Ollama images field,
    never written to a file. Falls back to the full virtual desktop if the
    window rect is degenerate, same fallback tools/_computer_vision.py's own
    _capture_screenshot_b64() uses."""
    rect = win32gui.GetWindowRect(hwnd)
    if rect[2] > rect[0] and rect[3] > rect[1]:
        img = ImageGrab.grab(bbox=rect)
    else:
        img = ImageGrab.grab(all_screens=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


async def execute(question: str, app: str | None = None) -> str:
    hwnd = _resolve_target_hwnd(app)
    if hwnd is None:
        return f"Couldn't find a window for {app!r} - is it open?" if app else "No window currently has focus."

    title = win32gui.GetWindowText(hwnd)

    config = _screen_config()
    excluded = config.get("excluded_windows", [])
    if _is_excluded(title, excluded):
        return f"Refused: {title!r} matches this machine's excluded-windows list - never captured."

    max_items = config.get("max_text_items", 150)
    uia_texts = _gather_uia_text(hwnd, max_items)

    vision_model = _load_config().get("models", {}).get("vision", "")
    vision_description = ""
    if vision_model:
        try:
            image_b64 = _capture_region_b64(hwnd)
            messages = [{"role": "user", "content": _VISION_PROMPT.format(question=question), "images": [image_b64]}]
            raw = ""
            async for token in brain_client.stream(messages, model=vision_model):
                raw += token
            vision_description = raw.strip()
        except Exception as exc:
            vision_description = f"(vision description unavailable: {exc})"

    lines = [f"Window title (exact, via accessibility tree): {title!r}"]
    if uia_texts:
        lines.append("On-screen text (exact, via accessibility tree):")
        lines.extend(f"  - {t!r}" for t in uia_texts)
    else:
        lines.append("On-screen text: none readable via the accessibility tree for this window.")
    if vision_description:
        lines.append(f"\nVisual description (a vision model's impression, not exact - treat as \"it looks like...\", not \"it says...\"): {vision_description}")

    return "\n".join(lines)
