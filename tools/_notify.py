"""Windows toast notifications (PROMPTS.md A23) - the shared sending
primitive behind both tools/notify.py (agent-callable) and
services/daemon/output.py's notify output path (A11's proactive daemon was
missing exactly this output until now - CLI print was the only one that
existed). winotify wraps the real Windows Action Center toast API (WinRT
under the hood), not a legacy system-tray balloon tip - modern Windows
visually suppresses/collapses balloon tips far more aggressively than a
real toast.

Raises on a real send failure rather than swallowing it - each caller
decides for itself whether that should be fatal (tools/notify.py: yes, let
the dispatcher's own exception handling report it) or logged-and-continue
(services/daemon/output.py: a failed toast shouldn't take down the whole
announcement path).

toast_enabled() exists because of a real failure caught live during this
build, not a hypothetical: winotify's Notification.show() shells out to
PowerShell and returns cleanly whether or not Windows actually displays
anything - confirmed by testing with both an unregistered app_id and a
genuinely registered AUMID (Windows PowerShell's own, pulled from
`Get-StartApps`), neither producing a visible toast, then screenshotting to
check rather than trusting the non-exception return (this project's own
"verify before declaring done" rule, applied to a case that looked done).
The actual cause was `HKCU / SOFTWARE/Microsoft/Windows/CurrentVersion/PushNotifications / ToastEnabled = 0` -
Windows' own global notification toggle (Settings > System > Notifications)
was off for the whole machine, unrelated to app registration or this code.
Not corrected here - flipping a systemwide OS setting is outside this
module's business - only surfaced honestly, so a caller can tell a real
send from a send that can never visibly succeed."""

import winreg

from winotify import Notification

_APP_ID = "Cortana"
_TOAST_ENABLED_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\PushNotifications"


def toast_enabled() -> bool:
    """Reads Windows' own global toast toggle directly - the only way found
    to actually know whether a call to send() below can ever be visible,
    since show() itself gives no such signal. Fails open (True) if the key
    or value is missing rather than assuming disabled - Windows' own
    documented default is enabled, and a caller trusting a spurious False
    here would suppress a toast that might have worked fine."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _TOAST_ENABLED_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "ToastEnabled")
            return bool(value)
    except OSError:
        return True


def send(title: str, message: str) -> None:
    Notification(app_id=_APP_ID, title=title, msg=message, duration="short").show()
