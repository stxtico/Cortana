"""Shared Tesseract OCR helper for ocr.py/screen.py - not itself an
agent-callable tool. Tesseract isn't installed on this machine yet (checked
live: `where tesseract` finds nothing) - the user installs it themselves
(`winget install --id UB-Mannheim.TesseractOCR`), same "system-level installs
stay on the user's side" precedent as WSL2/ffmpeg/CortanaShell.

is_available() calls pytesseract.get_tesseract_version(), which actually
invokes the `tesseract` binary as a subprocess - not just checking that the
pytesseract package imports. Confirmed live: pytesseract imports fine with no
Tesseract installed at all (it's a pure-Python subprocess wrapper) and only
fails once you try to actually call it - a naive "does the import work" check
would have reported this tool available when it structurally cannot run
(CLAUDE.md rule 10: an is_available() check has to reflect the real,
callable state, not just "the package is on disk").

CLAUDE.md rule 10 also requires this check to be incapable of starting or
changing anything - get_tesseract_version() only ever runs a read-only
`tesseract --version`, never launches a resident process or touches any
file, so it's safe to call unconditionally on every run_agent() turn.

Never falls back to a vision model when OCR is unavailable - a confident
wrong transcription from a tier already measured fabricating (see
CLAUDE.md's model-limitations section and A22's vision-as-last-resort
tiering) is worse than reporting the honest gap.
"""

import asyncio

import pytesseract
from PIL import Image


async def is_available() -> bool:
    try:
        await asyncio.to_thread(pytesseract.get_tesseract_version)
        return True
    except Exception:
        return False


def extract_text(image: Image.Image) -> str:
    """Synchronous - only ever called from execute() paths already gated
    behind is_available() returning True, same pattern as tools/_outlook.py's
    get_namespace()."""
    return pytesseract.image_to_string(image).strip()
