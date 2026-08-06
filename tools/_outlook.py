"""Shared Outlook COM-automation helpers for calendar_read.py/email_read.py -
not itself an agent-callable tool. Zero credentials ever touch this module or
the model: COM automation talks to whatever account is already signed into the
Outlook desktop app on this machine - a capability, not a credential, per
CLAUDE.md's A9 rule (she never receives, stores, or types a password/token).

**is_available() must never be capable of starting or changing anything - see
CLAUDE.md's non-negotiable rules.** Found live, the hard way: the first version
of this check used win32com.client.Dispatch("Outlook.Application"), which
launches Outlook if it isn't already running. is_available() runs on every
single run_agent() call (services/brain/agent.py's _drop_unavailable_tools()) -
that would have silently started a real Outlook process on every agent turn
once this is wired into the live loop. Confirmed by actually launching it: the
process took over 30s to respond, showed no window, and relaunched itself once
after being killed.

Fixed to two side-effect-free checks, in order: (1) is OUTLOOK.EXE already a
running process (tasklist, read-only, no COM at all), and only if so, (2) does
win32com.client.GetActiveObject("Outlook.Application") succeed -
GetActiveObject (unlike Dispatch) ONLY attaches to an already-running COM
server; it raises rather than launching one. If Outlook isn't already open,
this tool stays dormant - permanently, if the user never opens Outlook - which
is the correct, safe failure mode. The actual read path (execute(), in
calendar_read.py/email_read.py) uses GetActiveObject too, for the same reason:
never launch Outlook from any code path in this module, not just the check.
"""

import asyncio


async def _outlook_process_running() -> bool:
    proc = await asyncio.create_subprocess_exec(
        "tasklist.exe", "/FI", "IMAGENAME eq OUTLOOK.EXE", "/NH",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    return b"OUTLOOK.EXE" in stdout.upper()


async def is_available() -> bool:
    if not await _outlook_process_running():
        return False
    try:
        import win32com.client
        win32com.client.GetActiveObject("Outlook.Application")
        return True
    except Exception:
        return False


def get_namespace():
    """Only ever called from execute() paths already gated behind
    is_available() returning True - GetActiveObject here can't launch
    anything either, same guarantee as the check above."""
    import win32com.client
    outlook = win32com.client.GetActiveObject("Outlook.Application")
    return outlook.GetNamespace("MAPI")
