"""media_keys (PROMPTS.md A23) - OS-level media key injection (play/pause,
next track, previous track). Works with any app that responds to the
standard Windows media keys (Spotify, YouTube in a browser tab, VLC, etc.)
with zero per-app integration, since these are the same hardware keys a
physical keyboard's media row sends, not an app-specific API call.

Same scan-code-resolved win32api.keybd_event pattern
tools/_computer_uia.py's focus_window() already established and validated
live - a bare virtual-key code with no scan code silently fails to reach
some consumers of injected input (found the hard way during A18's
kill-switch build; tools/_computer_input.py's own docstring has the full
diagnosis). Reused here rather than the `keyboard` package's own send() -
one proven injection mechanism in this codebase for this whole class of
operation, not two.

No REQUIRES_CONFIRMATION - the same class of harmless, instantly-reversible
local action as tools/notify.py; none of CLAUDE.md rule 4's four gated
categories (delete/send/spend/submit/unlock) cover "press a media key."
"""

import win32api
import win32con

REQUIRES_CONFIRMATION = False

_KEYS = {
    "play_pause": win32con.VK_MEDIA_PLAY_PAUSE,
    "next": win32con.VK_MEDIA_NEXT_TRACK,
    "previous": win32con.VK_MEDIA_PREV_TRACK,
}


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "media_keys",
            "description": (
                "Send a media-control key press to whatever app is currently handling "
                "media playback (Spotify, a YouTube tab, VLC, etc.) - the same keys a "
                "physical keyboard's media row sends, so this works without knowing which "
                "app is actually playing or needing any per-app setup."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(_KEYS.keys()),
                        "description": "'play_pause' toggles play/pause, 'next' skips to the next track, 'previous' goes to the previous track.",
                    },
                },
                "required": ["action"],
            },
        },
    }


def _send_key(vk: int) -> None:
    scan = win32api.MapVirtualKey(vk, 0)
    win32api.keybd_event(vk, scan, 0, 0)
    win32api.keybd_event(vk, scan, win32con.KEYEVENTF_KEYUP, 0)


async def execute(action: str) -> str:
    vk = _KEYS.get(action)
    if vk is None:
        return f"Error: unknown action {action!r}. Valid: {', '.join(_KEYS.keys())}."
    _send_key(vk)
    return f"Sent media key: {action}."
