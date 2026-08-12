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
    app = pywinauto.Application(backend="uia")
    try:
        app.connect(path=process_match)
    except Exception:
        try:
            app.connect(title_re=f".*{process_match}.*")
        except Exception:
            return None

    for window in app.windows():
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
                process_name=process_match,
            )
    return None
