"""ocr (PROMPTS.md A24) - image to text via Tesseract, whitelisted to
[tools].whitelist_dirs (tools/_fs.py) same as read_file/pdf_read. Read-only,
no confirmation gate.

Preferred over the vision model wherever the answer is text - but "preferred"
is not "exact," measured live against a real screenshot of this session's own
window (small, anti-aliased real UI text, not a clean synthetic render):
mostly right, with real word-boundary errors ("Runit" for "Run it", "cloneon"
for "clone on"). Character recognition failing on adjacent-glyph spacing is a
fundamentally different, more trustworthy failure mode than the vision
model's measured fabrication rate (CLAUDE.md's Known model limitations
section) - OCR degrades toward garbled-but-recognizable, vision degrades
toward confidently wrong - but it's real error, not "exact," and this
module's own docstring/output shouldn't claim otherwise. Same tiering
discipline A22 established for UIA over vision, one rung down in confidence
from UIA (which IS exact - a real string value, not a recognition guess).
tools/screen.py's look_at_screen calls tools/_ocr.py directly for that reason
rather than routing through this wrapper's file-path interface.

Dormant until Tesseract is installed (tools/_ocr.py) - see that module's
docstring for the winget command and why is_available() has to actually
invoke the binary, not just check that pytesseract imports.
"""

from PIL import Image

from tools import _fs, _ocr


async def is_available() -> bool:
    return await _ocr.is_available()


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "ocr",
            "description": (
                "Extract text from an image file via Tesseract OCR - use this instead of "
                "look_at_screen's vision description whenever the goal is reading text (a "
                "screenshot, a scanned document, a photo of a sign). Real character recognition "
                "against real pixels, not a vision model's semantic guess - a vision model can "
                "fabricate text it can't actually read clearly, OCR can't invent content that "
                "isn't there, though it can still misread small or dense text (occasional "
                "word-boundary/spacing errors, not fabrication). Only files inside these "
                f"directories (and their subdirectories) are accessible: {_fs.whitelist_description()}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the image file, absolute or relative to the project root."},
                },
                "required": ["path"],
            },
        },
    }


async def execute(path: str) -> str:
    resolved = _fs.resolve_in_whitelist(path)
    if not resolved.exists():
        return f"No such file: {resolved}"
    if not resolved.is_file():
        return f"Not a file: {resolved}"

    image = Image.open(resolved)
    text = _ocr.extract_text(image)
    return text if text else "(no text detected in this image)"
