"""ocr (PROMPTS.md A24) - image to text via Tesseract, whitelisted to
[tools].whitelist_dirs (tools/_fs.py) same as read_file/pdf_read. Read-only,
no confirmation gate.

Preferred over the vision model wherever the answer is text: OCR is exact,
the vision model has a measured fabrication rate on this project (CLAUDE.md's
Known model limitations section) - same tiering discipline A22 already
established for UIA over vision. tools/screen.py's look_at_screen calls
tools/_ocr.py directly for that reason rather than routing through this
wrapper's file-path interface.

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
                "Extract exact text from an image file via Tesseract OCR - use this instead "
                "of look_at_screen's vision description whenever the goal is reading text "
                "(a screenshot, a scanned document, a photo of a sign), since OCR is exact and "
                "a vision model can fabricate text it can't actually read clearly. Only files "
                f"inside these directories (and their subdirectories) are accessible: "
                f"{_fs.whitelist_description()}."
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
