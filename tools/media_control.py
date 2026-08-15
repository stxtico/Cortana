"""media_control (PROMPTS.md A23 follow-up, built after media_keys' own
routing ambiguity was found and documented) - targets ONE specific app's
media session directly, via SMTC's own per-session control methods
(TryPlayAsync/TryPauseAsync/TrySkipNextAsync/TrySkipPreviousAsync -
tools/_smtc.py's control_session()), not a hardware key Windows routes on
its own heuristic. No routing ambiguity at all: this calls a method on a
specific session object identified by app id, not a keypress that gets
sent wherever Windows currently considers "active" - the structural fix
tools/media_keys.py cannot offer no matter how its verification is fixed,
because a hardware media key has no app-targeting concept to begin with.

Complements, doesn't replace, tools/media_keys.py: media_keys is the "just
pause whatever's playing" tool, useful precisely because it needs no app
name - the tradeoff, confirmed live and documented in that module's own
docstring, is that on a machine with more than one media source it can hit
the wrong app, or more than one at once. media_control is for the case
where the app is actually known (from all_sessions()/media_keys' own
result, or the user naming it directly - "pause Spotify") and precision
matters more than not having to name it.

Even here, "no routing ambiguity" only covers the TARGETED call - it
doesn't guarantee no OTHER session reacts as a side effect (apps can react
to each other's state changes for reasons outside this tool's control).
execute() diffs every session before/after, same discipline as
media_keys.execute(), and reports an unexpected other-session change
explicitly rather than assuming a targeted call is automatically
side-effect-free just because it's structurally more precise than a
hardware key.

REQUIRES_CONFIRMATION = False - same reasoning as media_keys and notify: a
fully local, instantly-reversible action, none of CLAUDE.md rule 4's four
gated categories.
"""

from tools import _smtc

REQUIRES_CONFIRMATION = False


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "media_control",
            "description": (
                "Control a SPECIFIC app's media playback directly (play, pause, next track, "
                "previous track) - targets exactly that app's session with no ambiguity about "
                "which app is affected, unlike media_keys. Use this whenever the app is known "
                "(the user named it, e.g. 'pause Spotify', or a prior media_keys/session check "
                "already showed which apps are active). `app` must be an exact app identifier "
                "SMTC currently knows about (e.g. 'Spotify.exe', 'Chrome.UserData.Profile1') - "
                "an unrecognized one is reported as not found, never guessed at."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {"type": "string", "description": "The exact app identifier to control, as reported by media_keys or a prior session check."},
                    "action": {
                        "type": "string",
                        "enum": list(_smtc.CONTROL_ACTIONS),
                        "description": "'play', 'pause', 'next', or 'previous'.",
                    },
                },
                "required": ["app", "action"],
            },
        },
    }


async def execute(app: str, action: str) -> str:
    if action not in _smtc.CONTROL_ACTIONS:
        return f"Error: unknown action {action!r}. Valid: {', '.join(_smtc.CONTROL_ACTIONS)}."

    sessions_before = _smtc.all_sessions()
    if app not in sessions_before:
        known = ", ".join(sorted(sessions_before)) or "(none)"
        return f"Error: no active session found for {app!r}. Currently known apps: {known}."

    ok, detail = _smtc.control_session(app, action)
    if not ok:
        return f"Failed to {action} {app!r}: {detail}."

    sessions_after = _smtc.all_sessions()
    changed = []
    for a in sorted(set(sessions_before) | set(sessions_after)):
        before = sessions_before.get(a, "(no session)")
        after = sessions_after.get(a, "(session gone)")
        if before != after:
            changed.append((a, before, after))

    target_change = next((c for c in changed if c[0] == app), None)
    others = [c for c in changed if c[0] != app]

    if target_change:
        result = f"Sent {action!r} to {app!r} directly: {target_change[1]} -> {target_change[2]}."
    else:
        result = f"Sent {action!r} to {app!r} directly (it reported success), but its own playback status didn't visibly change."

    if others:
        other_detail = "; ".join(f"{a!r} ({before} -> {after})" for a, before, after in others)
        result += f" Note: other session(s) also changed unexpectedly: {other_detail}."

    return result
