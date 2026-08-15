"""window_list (PROMPTS.md A23) - open windows with titles and owning
process. Reuses tools/_computer_uia.py's find_top_level_hwnds() - the same
real top-level-window enumeration A22 Step 1 built and fixed (real
win32gui.EnumWindows, filtered to visible/titled/non-desktop windows) -
called with an empty process_match string rather than adding a second
enumeration. find_top_level_hwnds()'s own filter is a plain substring check
(`process_match.lower() not in proc_name.lower()`), and an empty string is
a substring of every string, so "" matches every process without any
change to that function - "reuse the enumeration that already exists," not
a new one.
"""

import psutil
import win32gui
import win32process

from tools import _computer_uia

REQUIRES_CONFIRMATION = False


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "window_list",
            "description": "List currently open, visible top-level windows with their titles and owning application.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


async def execute() -> str:
    hwnds = _computer_uia.find_top_level_hwnds("")
    rows = []
    for hwnd in hwnds:
        title = win32gui.GetWindowText(hwnd)
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process_name = psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            process_name = "(unknown)"
        rows.append((title, process_name))

    if not rows:
        return "No open windows found."
    return "\n".join(f"{title!r} - {process}" for title, process in rows)
