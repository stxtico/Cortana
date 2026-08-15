"""System Media Transport Controls query (PROMPTS.md A23 - media_keys
correctness fix). The same WinRT API Windows' own taskbar media widget
reads from, and the actual source of truth for which app a media key press
will affect. Shells out to PowerShell for the WinRT call (same pattern
tools/_notify.py's winotify dependency already uses for toast display)
rather than adding a new winsdk/winrt Python dependency for one query.

Built after a real, live-caught bug: the first media_keys verification
queried "the current session" before and after sending a key and compared
only PlaybackStatus, silently assuming it was the same app both times. It
wasn't - the pause landed on a YouTube tab, and by the time of the next
query the current session had shifted to Spotify (SMTC's notion of
"current" tracks recent activity, not a fixed target), so what looked like
a clean Playing -> Paused -> Playing round trip was actually two different
apps: a video left paused and an unrelated app resumed. Every function here
returns the session's source app alongside its playback state for exactly
this reason - never one without the other, so a caller can never repeat
that mistake by construction.
"""

import subprocess

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
