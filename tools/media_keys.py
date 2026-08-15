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
caller - this tool included - can choose or force. Considered and
rejected: an `app` parameter that focuses a target window first, on the
theory that focus would redirect the key - it wouldn't. SMTC session
routing tracks playback activity, not OS window focus, so focusing a
window doesn't influence which session Windows treats as current; a
parameter that implied otherwise would be a false promise, not a real fix.

Verifying this went through three real, live-caught failures before
landing correctly, each one worth keeping:
1. Comparing only ONE session's PlaybackStatus before/after a key press
   (not tracking which app it belonged to at all) reported a confident,
   clean round trip that was actually two different apps - a pause that
   hit a real YouTube tab, and a later "restore" key that hit Spotify
   instead, because "current" had shifted between calls.
2. Fixed to re-check one specific app's own session directly instead of
   trusting "current" - better, but still checking only ONE app. On this
   machine a single play_pause was then observed to change TWO sessions in
   opposite directions in a single call (paused a playing YouTube tab AND
   resumed an already-paused Spotify at once) - the one-app check reported
   this as a confident, correct single-app result, because the one app it
   happened to check genuinely had changed. It just wasn't the only one,
   and the check had no way to know that.
3. Fixed properly: execute() now diffs EVERY session that existed before
   or after, not one - reports a single confirmed app only when exactly
   one session actually changed, reports all of them plainly when more
   than one did, and treats "more than one session changed from a single
   key press" as a real, load-bearing finding about this tool's
   reliability on a multi-source machine, not an edge case to explain away.
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
                "than one media source open (e.g. a music app and a browser tab), this can "
                "affect a different app than the one meant, or - confirmed on this machine - "
                "more than one app at once in opposite directions from a single press. The "
                "result names every app it confirmed actually changed - read all of it, don't "
                "assume only the intended app was hit or that only one app was affected."
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

    active_before, _ = _smtc.current_session()
    sessions_before = _smtc.all_sessions()
    _send_key(vk)
    await asyncio.sleep(_settle_delay_s())
    sessions_after = _smtc.all_sessions()

    # Diff EVERY session that existed on either side, not just whichever one
    # was "active" beforehand - checking only one session's own state was a
    # real bug, not just an oversight: on this machine, a single play_pause
    # was observed to change TWO sessions in opposite directions in one call
    # (paused a YouTube tab that was playing, resumed an already-paused
    # Spotify), and a single-session check reported that as a confident,
    # correct single-app result because the one session it happened to check
    # really had changed - it just wasn't the only one. Only a full diff can
    # catch that; see tools/_smtc.py's docstring for the live-caught history.
    changed = []
    for app in sorted(set(sessions_before) | set(sessions_after)):
        before = sessions_before.get(app, "(no session)")
        after = sessions_after.get(app, "(session gone)")
        if before != after:
            changed.append((app, before, after))

    if not changed:
        return f"Sent media key: {action!r}. No session's playback state changed - this key press may not have reached anything, or its effect isn't reflected in playback status."

    if len(changed) == 1:
        app, before, after = changed[0]
        if app == active_before:
            return f"Sent media key: {action!r}. Confirmed: affected {app!r} ({before} -> {after}), the session that was active beforehand."
        return (
            f"Sent media key: {action!r}. Affected {app!r} ({before} -> {after}), but "
            f"{active_before!r} was the session Windows considered active beforehand - the key "
            "reached a different app than expected."
        )

    detail = "; ".join(f"{app!r} ({before} -> {after})" for app, before, after in changed)
    return (
        f"Sent media key: {action!r}. More than one session changed state: {detail}. On this "
        "machine a single key press can affect multiple apps at once, not only the one Windows "
        "considers active - a single-app attribution from this tool cannot be trusted here. "
        "Report all of the affected apps, not just one."
    )
