"""Windows UI Automation target resolution (PROMPTS.md A18) - the primary
resolution tier tools/computer.py uses, ahead of Playwright/CLI/vision. Not
itself an agent-callable tool, same "helper module, not a tool module" shape
as tools/_fs.py and tools/_outlook.py.

Why this tier exists at all, and why it's first: A14's CLAUDE.md entry found
the vision model this project uses fabricates specific wrong claims rather
than admitting uncertainty, and separately produced a real false negative
(missed 3-of-4 wrong hole count) even in an already-hardened setup. Driving a
GUI by resolving real control identity (name, control type, automation ID,
bounding rect) through the accessibility tree that Windows itself exposes is
categorically more reliable than asking a model to point at pixels - the
click target comes from the OS's own knowledge of what's actually there, not
a guess. Built on pywinauto's backend="uia" (COM-based UI Automation, not the
older/weaker Win32 backend - UIA is what modern apps, browsers, and Win32
apps with UIA providers all expose).
"""

import os
from dataclasses import dataclass

import psutil
import pywinauto
import win32api
import win32con
import win32gui
import win32process


@dataclass
class ResolvedElement:
    name: str
    control_type: str
    automation_id: str
    center_x: int
    center_y: int
    is_password: bool
    process_name: str
    hwnd: int  # the top-level window's real handle - what focus_window() below actually foregrounds


def focus_window(hwnd: int) -> bool:
    """Attempts to bring hwnd to the real OS foreground, returns whether it
    actually landed there - a best-effort establish, not a guarantee.
    tools/computer.py's execute() still runs the existing live
    foreground_process_name() re-check after calling this and refuses
    exactly as before if it didn't land or landed on the wrong process; this
    function only changes whether she has to depend on the user already
    having focused the target, not what happens when she can't.

    A bare win32gui.SetForegroundWindow() from a background process is
    unreliable by Windows' own design (the well-known foreground-lock
    protection) - confirmed empirically, not assumed: three real attempts
    from this exact process context, no synthetic input sent beforehand,
    succeeded once and failed twice with the documented error 258
    ("the wait operation timed out" - Windows' actual wording for the
    foreground-lock refusal). The standard, legitimate fix - not a
    workaround that papers over the restriction, but one that genuinely
    satisfies it - is sending one real synthetic key event first, which
    registers this process as having "recent input" in Windows' own terms,
    one of the documented conditions SetForegroundWindow honors. Re-verified
    3/3 with this fix in place before shipping it. Uses the same
    scan-code-resolved keybd_event tools/_computer_input.py's own docstring
    documents as load-bearing (a bare virtual-key code with no scan code
    silently fails to reach some consumers of injected input, found during
    the kill-switch build)."""
    VK_MENU = 0x12
    scan = win32api.MapVirtualKey(VK_MENU, 0)
    win32api.keybd_event(VK_MENU, scan, 0, 0)
    win32api.keybd_event(VK_MENU, scan, win32con.KEYEVENTF_KEYUP, 0)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        return False
    return win32gui.GetForegroundWindow() == hwnd


def foreground_process_name() -> str:
    """The process owning the current foreground window, by name (e.g.
    'explorer.exe') - what tools/computer.py's per-application allowlist
    check compares against, live, before every action (not cached from task
    start - see that module's allowlist enforcement). Uses pywin32 only
    (already a real project dependency, tools/_outlook.py's COM work) rather
    than psutil - psutil resolves in this environment only as an undeclared
    transitive dependency of something else, which would silently break if
    that something else ever dropped it."""
    hwnd = win32gui.GetForegroundWindow()
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid)
    try:
        return os.path.basename(win32process.GetModuleFileNameEx(handle, 0))
    finally:
        win32api.CloseHandle(handle)


def _is_password(element_info) -> bool:
    """Checks UI Automation's own IsPassword property (UIA_IsPasswordPropertyId,
    exposed by pywinauto as .element.CurrentIsPassword on the raw COM element) -
    a structural property of the control itself, not a heuristic over what text
    might be typed into it. This is the actual mechanism the "never type a
    password" refusal in tools/computer.py depends on: refuse before any
    keystroke synthesis if this is True, unconditionally, no confirmation
    offered (the instruction was "never," not "confirm first").

    Defensive try/except: not every element_info implementation (legacy
    Win32-backend fallback elements pywinauto can still surface even under
    backend="uia") necessarily has .element / CurrentIsPassword. Treated as
    "can't confirm it's safe" -> True (refuse), not False (allow) - failing
    closed on an unknown control, not open, matches this project's password
    rule being a hard "never," not a best-effort one."""
    try:
        return bool(element_info.element.CurrentIsPassword)
    except Exception:
        return True


def _walk(element, max_depth: int = 12):
    """Manual depth-first traversal via .children(), not pywinauto's
    single-shot .descendants(). Found live, the hard way, chasing a real miss
    on a real Windows Explorer window: .descendants()'s single
    FindAll(TreeScope.Descendants) issued from the top-level window doesn't
    reach Explorer's virtualized "Items View" list contents at all - it found
    the List container itself but zero ListItem children underneath it -
    while calling .children() directly on that same List element returned
    the real items immediately (confirmed: 'models', 'cortana', 'persona',
    'profile', live folder/file names). This is a real, documented-in-
    practice UIA quirk with modern (DirectUI-hosted) Explorer list views, not
    a hostile control that genuinely can't be walked - it needs realization
    triggered at each level via an explicit .children() call, not assumed
    reachable from one distant top-down query. max_depth is a sane recursion
    guard, not a limit actually hit in practice - Explorer's real structure
    is shallow."""
    yield element
    if max_depth <= 0:
        return
    try:
        children = element.children()
    except Exception:
        return
    for child in children:
        yield from _walk(child, max_depth - 1)


def _name_matches(info_name: str, target: str) -> bool:
    """Exact match, or a match ignoring a file extension - Explorer commonly
    hides known extensions by default (confirmed live: a real "cortana.toml"
    file surfaced with UIA Name "cortana", no extension at all), so a target
    given with its extension still needs to resolve against that."""
    if info_name == target:
        return True
    return info_name == target.rsplit(".", 1)[0]


def _find_top_level_hwnds(process_match: str) -> list[int]:
    """Real top-level window enumeration by owning-process name, not
    pywinauto's connect(path=process_match). Found live during A22's
    grounding benchmark: connect(path="Code.exe") raises
    ProcessNotFoundError outright - not "picks the wrong process," but
    total failure - for any app that spawns multiple processes sharing one
    executable name (VS Code, Chrome, Electron apps including cortana's own
    control panel all do this: renderer/helper/GPU processes alongside the
    one that actually owns a window). This was silently zeroing out UIA
    coverage on exactly those apps, not because they lack real UIA
    structure - direct enumeration this same way found 120 real elements in
    VS Code and 61 in Chrome - but because resolve() could never even reach
    them. explorer.exe and WindowsTerminal.exe (single real process each)
    happened to work fine under the old connect(path=...) call, which is
    why the bug wasn't obvious from casual testing on Explorer alone.

    Returns every matching top-level window's hwnd (not just one) - a
    process can legitimately own several real windows (multiple Explorer
    folders, multiple Chrome/VS Code windows), and the caller needs to try
    each rather than assume the first one found is the right one."""
    hwnds = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if not win32gui.GetWindowText(hwnd):
            return
        if win32gui.GetClassName(hwnd) == "Progman":
            return  # the desktop shell, also owned by explorer.exe - never a real app window
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc_name = psutil.Process(pid).name()
        except Exception:
            return
        if process_match.lower() not in proc_name.lower():
            return
        rect = win32gui.GetWindowRect(hwnd)
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            return
        hwnds.append(hwnd)

    win32gui.EnumWindows(_cb, None)
    return hwnds


def resolve(
    process_match: str,
    *,
    name: str | None = None,
    control_type: str | None = None,
    automation_id: str | None = None,
) -> ResolvedElement | None:
    """Finds a control within the window(s) owned by a process matching
    process_match (substring match against the process name, e.g. 'explorer'
    matches 'explorer.exe'), walking the real tree (see _walk()'s docstring
    for why that's a manual recursion, not pywinauto's descendants()) for one
    matching whichever of name/control_type/automation_id are given (all
    provided constraints must match - a real identity lookup, not a fuzzy
    search). Returns None if no window or no matching control is found -
    callers (computer.py) fall through to the next resolution tier on None,
    never on an exception from here (a resolution miss is an expected,
    ordinary outcome, not an error condition)."""
    for hwnd in _find_top_level_hwnds(process_match):
        app = pywinauto.Application(backend="uia")
        try:
            app.connect(handle=hwnd)
        except Exception:
            continue
        window = app.window(handle=hwnd)
        try:
            candidates = _walk(window)
        except Exception:
            continue
        for element in candidates:
            info = element.element_info
            if name is not None and not _name_matches(info.name or "", name):
                continue
            if control_type is not None and info.control_type != control_type:
                continue
            if automation_id is not None and info.automation_id != automation_id:
                continue
            try:
                rect = info.rectangle
            except Exception:
                continue
            center_x = (rect.left + rect.right) // 2
            center_y = (rect.top + rect.bottom) // 2
            return ResolvedElement(
                name=info.name or "",
                control_type=info.control_type or "",
                automation_id=info.automation_id or "",
                center_x=center_x,
                center_y=center_y,
                is_password=_is_password(info),
                hwnd=window.handle,
                process_name=process_match,
            )
    return None
