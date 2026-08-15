"""Windows clipboard read/write (PROMPTS.md A23) - shared primitive behind
tools/clipboard_read.py and tools/clipboard_write.py, via win32clipboard
(already a pywin32 dependency - no new package needed).
"""

import win32clipboard


def read_text() -> str | None:
    """Returns the clipboard's current text, or None if it holds no text
    (empty, or a non-text format like an image/file list) - a real, ordinary
    state, not an error."""
    win32clipboard.OpenClipboard()
    try:
        if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            return None
        return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()


def write_text(text: str) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()
