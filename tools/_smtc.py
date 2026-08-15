"""System Media Transport Controls query (PROMPTS.md A23 - media_keys
correctness fix). The same WinRT API Windows' own taskbar media widget
reads from, and the actual source of truth for which app a media key press
will affect. Shells out to PowerShell for the WinRT call (same pattern
tools/_notify.py's winotify dependency already uses for toast display)
rather than adding a new winsdk/winrt Python dependency for one query.

Built after real, live-caught bugs in media_keys' own verification, in two
stages: first, comparing "the current session"'s PlaybackStatus before and
after a key without tracking which app it belonged to reported a clean
round trip that was actually two different apps (a pause that landed on a
YouTube tab, a later key that hit Spotify instead, because SMTC's notion of
"current" had shifted between calls). Fixed to check one specific app's own
session directly - still wrong, because on this machine a single key press
was then observed to change TWO sessions in opposite directions at once
(paused a playing YouTube tab and resumed an already-paused Spotify
together), and checking only one app reported that as a confident,
correct single-app result. Every function here returns the session's
source app alongside its playback state, and callers (tools/media_keys.py)
are expected to diff EVERY session that exists before or after, not any
one of them - the only way found so far not to repeat either mistake.
"""

import subprocess
import tempfile
from pathlib import Path

_PS_PREAMBLE = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
Function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media.Control,ContentType=WindowsRuntime] | Out-Null
$manager = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
"""

_CURRENT_SESSION_SCRIPT = _PS_PREAMBLE + r"""
$session = $manager.GetCurrentSession()
if ($session) {
    $playback = $session.GetPlaybackInfo()
    Write-Output "$($session.SourceAppUserModelId)|$($playback.PlaybackStatus)"
} else {
    Write-Output "|NoSession"
}
"""

_ALL_SESSIONS_SCRIPT = _PS_PREAMBLE + r"""
foreach ($session in $manager.GetSessions()) {
    $playback = $session.GetPlaybackInfo()
    Write-Output "$($session.SourceAppUserModelId)|$($playback.PlaybackStatus)"
}
"""

# Takes AppId/Action as real script parameters (param() block), never
# string-interpolated into the script's own source text - so a caller
# passing an untrusted app id or action (this tool's `app` argument
# ultimately comes from a model's tool call) can never be interpreted as
# PowerShell code, only ever compared as a plain string value. Same
# "constrain the shape, don't trust the content" reasoning as every other
# externally-sourced value flowing into a shell-adjacent call in this
# codebase (e.g. tools/shell.py's whitelist).
_CONTROL_SCRIPT = r"""
param(
    [string]$AppId,
    [string]$Action
)
""" + _PS_PREAMBLE + r"""
$target = $manager.GetSessions() | Where-Object { $_.SourceAppUserModelId -eq $AppId } | Select-Object -First 1
if (-not $target) {
    Write-Output "NOTFOUND"
    exit
}
switch ($Action) {
    "play"     { $ok = Await ($target.TryPlayAsync()) ([bool]) }
    "pause"    { $ok = Await ($target.TryPauseAsync()) ([bool]) }
    "next"     { $ok = Await ($target.TrySkipNextAsync()) ([bool]) }
    "previous" { $ok = Await ($target.TrySkipPreviousAsync()) ([bool]) }
    default    { Write-Output "BADACTION"; exit }
}
Write-Output "OK:$ok"
"""

CONTROL_ACTIONS = ("play", "pause", "next", "previous")


def current_session(timeout_s: float = 10.0) -> tuple[str | None, str]:
    """Returns (source_app_id, playback_status) for whatever SMTC currently
    considers the active session - (None, "NoSession") if there isn't one.
    Synchronous and blocking (not fire-and-forget like tools/_notify.py's
    send()) - callers need the real answer before deciding what to report,
    not just confirmation that a command was issued.

    source_app_id is an AUMID-shaped string (e.g. "Spotify.exe",
    "Chrome.UserData.Profile1"), not a pretty name - good enough to compare
    for identity (same app or not) and to show a human, not meant to be the
    only thing shown to one.

    Not sufficient on its own to confirm what a media key affected -
    confirmed live: "current" can reassign to a completely untouched app
    the instant the app that was actually just paused stops being the most
    recently active one. Use all_sessions() to check a specific app's own
    state directly instead."""
    result = subprocess.run(
        ["powershell.exe", "-ExecutionPolicy", "Bypass", "-Command", _CURRENT_SESSION_SCRIPT],
        capture_output=True, text=True, timeout=timeout_s,
    )
    line = result.stdout.strip()
    if "|" not in line:
        return None, "Unknown"
    app_id, status = line.split("|", 1)
    return (app_id or None), status


def all_sessions(timeout_s: float = 10.0) -> dict[str, str]:
    """Returns {source_app_id: playback_status} for every session SMTC
    currently knows about, not just the ambiguous "current" one. Exists
    specifically so a caller can re-check ONE SPECIFIC app's own state
    after sending a media key, rather than trusting whatever "current"
    happens to point at afterward - confirmed live: a key correctly paused
    a real YouTube tab, but the very next current_session() query had
    already handed "current" to Spotify, whose own state never changed at
    all. Checking a specific app's own entry here is direct evidence of
    what changed; current_session() alone isn't, once "current" has
    already moved on."""
    result = subprocess.run(
        ["powershell.exe", "-ExecutionPolicy", "Bypass", "-Command", _ALL_SESSIONS_SCRIPT],
        capture_output=True, text=True, timeout=timeout_s,
    )
    sessions = {}
    for line in result.stdout.strip().splitlines():
        if "|" not in line:
            continue
        app_id, status = line.split("|", 1)
        if app_id:
            sessions[app_id] = status
    return sessions


def control_session(app_id: str, action: str, timeout_s: float = 10.0) -> tuple[bool, str]:
    """Calls `action` directly on app_id's own SMTC session
    (TryPlayAsync/TryPauseAsync/TrySkipNextAsync/TrySkipPreviousAsync) - no
    routing ambiguity at all, unlike a raw hardware media key
    (tools/media_keys.py), because this targets one specific session
    object by identity rather than sending a keypress Windows itself
    decides where to route.

    Returns (ok, detail). ok is True only when the session was found AND
    its own Try*Async call reported success - these calls can legitimately
    fail (not every app implements every control), so finding a matching
    session is not itself proof the action succeeded.

    Writes _CONTROL_SCRIPT to a real temp file and invokes it with -File
    plus separate -AppId/-Action parameters, not a -Command string built
    from interpolating those values - see _CONTROL_SCRIPT's own comment
    for why."""
    if action not in CONTROL_ACTIONS:
        return False, f"unknown action {action!r} - valid: {', '.join(CONTROL_ACTIONS)}"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".ps1", delete=False, encoding="utf-8") as f:
        f.write(_CONTROL_SCRIPT)
        script_path = f.name

    try:
        result = subprocess.run(
            ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", script_path, "-AppId", app_id, "-Action", action],
            capture_output=True, text=True, timeout=timeout_s,
        )
    finally:
        Path(script_path).unlink(missing_ok=True)

    output = result.stdout.strip()
    if output == "NOTFOUND":
        return False, f"no active session found for {app_id!r}"
    if output == "BADACTION":
        return False, f"unrecognized action {action!r}"
    if output.startswith("OK:"):
        ok = output[3:].strip().lower() == "true"
        return ok, ("succeeded" if ok else "the app reported it could not perform this action")
    stderr = result.stderr.strip()
    return False, f"unexpected output: {output!r}" + (f" (stderr: {stderr})" if stderr else "")
