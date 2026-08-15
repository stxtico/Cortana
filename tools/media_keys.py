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

A real, inherent limitation, surfaced rather than hidden or worked around:
Windows routes a media key to whichever app it currently considers the
"active" media session (tools/_smtc.py - the System Media Transport
Controls API), a heuristic based on recent activity, not something any
caller - this tool included - can choose or force. Caught live, the hard
way: a first verification pass paused a YouTube tab, then a second call
meant to un-pause it instead resumed Spotify, because the "active" session
had shifted between the two calls. Considered and rejected: an `app`
parameter that focuses a target window first, on the theory that focus
would redirect the key - it wouldn't. SMTC session routing tracks playback
activity, not OS window focus, so focusing a window doesn't influence which
session Windows treats as current; a parameter that implied otherwise would
be a false promise, not a real fix. What execute() actually does instead:
records which app was the active session before sending the key
(tools/_smtc.py), then re-checks THAT SPECIFIC app's own playback state
afterward (not just whichever session is "current" by then, which turned
out to be a second, related trap - "current" reassigned to an untouched
Spotify the instant the correctly-paused YouTube tab stopped being most
recently active, which would have read as a false "wrong app" result on a
key press that actually worked fine). A confirmed status change on the
original app is reported as confirmed; anything else - no change, or the
session having disappeared - is reported as genuine uncertainty, not
guessed at.
"""

import asyncio
import tomllib
from pathlib import Path

import win32api
import win32con

from tools import _smtc

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

REQUIRES_CONFIRMATION = False

_KEYS = {
    "play_pause": win32con.VK_MEDIA_PLAY_PAUSE,
    "next": win32con.VK_MEDIA_NEXT_TRACK,
    "previous": win32con.VK_MEDIA_PREV_TRACK,
}


def _settle_delay_s() -> float:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("tools", {}).get("media_keys", {}).get("settle_delay_s", 0.4)


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "media_keys",
            "description": (
                "Send a media-control key press - the same keys a physical keyboard's media "
                "row sends, with zero per-app setup. Real limitation: Windows routes this key "
                "to whichever app it currently considers the active media session, based on "
                "recent activity - not something this tool can choose. On a machine with more "
                "than one media source open (e.g. a music app and a browser tab both playing "
                "or recently active), this can affect a different app than the one meant. The "
                "result tells you which app it believes was actually affected - check it "
                "rather than assuming the intended app was hit."
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

    app_before, status_before = _smtc.current_session()
    _send_key(vk)
    await asyncio.sleep(_settle_delay_s())

    if app_before is None:
        return f"Sent media key: {action!r}. No active media session was detected beforehand - can't say what, if anything, this reached."

    # Re-check app_before's OWN session directly, not just whichever session
    # is "current" now - "current" can reassign to an untouched app the
    # instant the one actually just affected stops being most recently
    # active (confirmed live - see tools/_smtc.py's docstring), which would
    # otherwise read as a false "wrong app" result on a key that worked fine.
    sessions_after = _smtc.all_sessions()
    status_after = sessions_after.get(app_before)

    if status_after is None:
        return (
            f"Sent media key: {action!r}. {app_before!r} was the active session beforehand "
            f"({status_before}), but no session for it exists anymore afterward - it may have "
            "closed. Can't confirm what this actually affected."
        )

    if status_after != status_before:
        return f"Sent media key: {action!r}. Confirmed: affected {app_before!r} ({status_before} -> {status_after})."

    current_after, current_status_after = _smtc.current_session()
    return (
        f"Sent media key: {action!r}. {app_before!r}'s playback status is unchanged "
        f"({status_before}) - this key press may not have reached it. Windows' active session "
        f"is now {current_after!r} ({current_status_after}), which may be what it actually "
        "affected instead. Can't confirm which app this reached - report that rather than "
        "assuming either one."
    )
