"""Shared Tesseract OCR helper for ocr.py/screen.py - not itself an
agent-callable tool. Installed via `winget install --id UB-Mannheim.TesseractOCR
--silent` (system-level install, same "stays on the user's side" precedent as
WSL2/ffmpeg/CortanaShell) - but confirmed live that winget's silent mode
doesn't tick the installer's "add to PATH" step: the binary installs cleanly
and runs correctly (verified: an exact-match OCR round-trip against a
known-text test image) at its standard location, `where tesseract` still
finds nothing, and a fresh process's PATH lookup keeps failing. Not a broken
install - a PATH gap a re-clone or a future machine would hit identically.

is_available() has to mean "this will actually work," not "this is on PATH" -
those turned out to be different things here. _candidate_commands() tries
PATH first (shutil.which), then the standard UB-Mannheim install location as
a fallback - but a fallback path existing on disk isn't proof it's callable
either (wrong architecture, blocked, corrupted download), so every candidate
is verified the same way: point pytesseract at it and actually call
get_tesseract_version() (a real subprocess invocation), not just check the
file exists. The first candidate that actually responds wins and is left
configured on pytesseract.pytesseract.tesseract_cmd for extract_text() below
to reuse - same "verify the real thing, not a proxy for it" reasoning this
module already used once for "package imports" vs "binary is callable."

CLAUDE.md rule 10 requires this check to be incapable of starting or changing
anything in the world - get_tesseract_version() only ever runs a read-only
`tesseract --version`, and pointing pytesseract's own module-level
tesseract_cmd at a resolved path is local Python config, not a side effect on
the machine - so this stays safe to call unconditionally on every
run_agent() turn, same as before.

Never falls back to a vision model when OCR is unavailable - a confident
wrong transcription from a tier already measured fabricating (see
CLAUDE.md's model-limitations section and A22's vision-as-last-resort
tiering) is worse than reporting the honest gap.
"""

import asyncio
import shutil
from pathlib import Path

import pytesseract
from PIL import Image

_FALLBACK_PATHS = [
    Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
]


def _candidate_commands() -> list[str]:
    """Every plausible way to invoke tesseract, PATH first. Only returns
    things that actually resolve to something on disk - never a bare
    "tesseract" guess with nothing behind it, which would just be the same
    proxy check this module is trying to avoid."""
    candidates = []
    on_path = shutil.which("tesseract")
    if on_path:
        candidates.append(on_path)
    for p in _FALLBACK_PATHS:
        if p.exists() and str(p) not in candidates:
            candidates.append(str(p))
    return candidates


def _check_command(cmd: str) -> None:
    """Synchronous. Raises if `cmd` isn't genuinely callable - the caller
    decides what that means."""
    pytesseract.pytesseract.tesseract_cmd = cmd
    pytesseract.get_tesseract_version()


def _resolve() -> str | None:
    """Synchronous - tries every candidate, returns the first one that's
    genuinely callable (leaving pytesseract configured to use it), or None
    if nothing worked. The single source of truth both is_available() and
    extract_text() go through - deliberately NOT split into "is_available()
    resolves, extract_text() trusts a global someone else set," which is
    exactly the coupling that broke calling ocr.execute() directly in a
    fresh process during this fix's own verification (is_available() had
    never run in that process, so pytesseract.pytesseract.tesseract_cmd was
    still its unresolved "tesseract" default, and image_to_string() raised
    a raw TesseractNotFoundError instead of trying the fallback path at
    all). Re-resolving here every call costs one extra `--version`
    subprocess invocation - real but small, and this isn't a hot path."""
    for cmd in _candidate_commands():
        try:
            _check_command(cmd)
            return cmd
        except Exception:
            continue
    return None


async def is_available() -> bool:
    return await asyncio.to_thread(_resolve) is not None


def extract_text(image: Image.Image) -> str:
    """Synchronous. Self-resolving - does not assume is_available() already
    ran in this process (see _resolve()'s docstring for why that assumption
    broke). Still only ever called from execute() paths meant to be gated by
    is_available() at the dispatcher/tool-list level (same pattern as
    tools/_outlook.py's get_namespace()) - this is a second, independent
    safety net, not a replacement for that gate."""
    if _resolve() is None:
        raise RuntimeError("Tesseract isn't available on this machine (checked PATH and the standard install location) - ocr should not have been offered as available.")
    return pytesseract.image_to_string(image).strip()
