"""Cancellable, cooperative OS-level mouse input synthesis (PROMPTS.md A18).
Not itself an agent-callable tool - the primitive tools/computer.py's execute()
sequences: walk -> working state -> this module's eased move -> pause -> click.

Built and verified FIRST, before any target-resolution or click-dispatch code,
per explicit instruction (PLAN.md Phase 9: "build the abort before you build
the click"). The reason this module exists at all, rather than a single
win32api.SetCursorPos() call per move: services/brain/agent_safety.py's global
abort hotkey cancels via asyncio.Task.cancel(), which only interrupts at an
await point. A single blocking OS call has no such point - the whole move
would complete before cancellation is even noticed, defeating the entire
"stop her mid-motion" requirement. move_cursor_eased() is instead a real
cooperative loop: many small steps, `await asyncio.sleep()` between each, so a
cancellation lands within one step (~15ms), not after the whole path.

Live-verified via scripts/computer_killswitch_test.py: real cursor motion, a
real hotkey press synthesized via raw win32 keybd_event (NOT the `keyboard`
package's own send() - that sets an internal is_replaying flag that makes its
own hook ignore its own synthetic events, confirmed live when a first attempt
at this test silently did nothing) mid-sweep, confirmed stopping immediately
with no stuck button state - see that script's own docstring for the full
diagnosis, including a second real bug (a missing scan code) found along the way.
"""

import asyncio

import win32api
import win32con

STEP_S = 0.015  # ~15ms/step - fine enough that a cancellation lands almost immediately, coarse enough not to spam the OS cursor API


def _ease_out_cubic(t: float) -> float:
    """Human-ish deceleration into the target, not linear - PLAN.md's own
    phrasing ("a human-ish eased path, not linear"). Cubic ease-out: fast
    start, slows into the landing point."""
    return 1.0 - (1.0 - t) ** 3


async def move_cursor_eased(target_x: int, target_y: int, duration_s: float = 0.5) -> None:
    """Moves the real OS cursor from its current position to (target_x, target_y)
    over duration_s, in SetCursorPos's absolute virtual-desktop pixel space (so
    this works correctly across monitors, not just the primary one - GetCursorPos/
    SetCursorPos already operate in that space, no monitor-aware math needed here).

    Cooperative by construction: awaits between every step, so
    asyncio.Task.cancel() (services/brain/agent_safety.py's abort hotkey) can
    land mid-move. Raises asyncio.CancelledError like any other cancelled
    coroutine - callers that need cleanup on abort (e.g. click()'s button-up)
    use try/finally, not a try/except here, since there's no OS state this
    function itself holds across a cancellation (unlike a held-down button)."""
    start_x, start_y = win32api.GetCursorPos()
    distance = max(abs(target_x - start_x), abs(target_y - start_y))
    if distance == 0:
        return
    steps = max(1, round(duration_s / STEP_S))
    for i in range(1, steps + 1):
        t = _ease_out_cubic(i / steps)
        x = round(start_x + (target_x - start_x) * t)
        y = round(start_y + (target_y - start_y) * t)
        win32api.SetCursorPos((x, y))
        await asyncio.sleep(STEP_S)


async def click(button: str = "left") -> None:
    """Presses and releases a mouse button at the cursor's current position.
    try/finally around the button-up, not just a happy-path sequence - a
    cancellation landing between down and up would otherwise leave the button
    physically held, a real stuck-input-state risk agent_safety.py's dispatcher
    doesn't clean up on its own (it only catches CancelledError and reports it;
    OS input state is this module's responsibility, not the dispatcher's)."""
    down_flag, up_flag = {
        "left": (win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP),
        "right": (win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP),
    }[button]
    win32api.mouse_event(down_flag, 0, 0, 0, 0)
    try:
        await asyncio.sleep(0.05)  # a real click has *some* down-time; 0ms reads as a synthetic event to some apps
    finally:
        win32api.mouse_event(up_flag, 0, 0, 0, 0)


def cursor_position() -> tuple[int, int]:
    return win32api.GetCursorPos()


async def type_char(ch: str) -> None:
    """Types one character via win32api.VkKeyScan (maps a Unicode character to
    the virtual-key + shift-state needed to produce it on the active
    keyboard layout) plus keybd_event with a real resolved scan code - same
    fix this module's docstring documents was load-bearing for the abort
    hotkey test (a bare vk with no scan code doesn't reliably reach every
    consumer of injected key events). One await per character, not a single
    blocking multi-character burst, for the same cooperative-cancellation
    reason move_cursor_eased() steps in small increments - tools/computer.py's
    execute() types text a character at a time so an abort mid-typed-string
    still lands promptly instead of only between whole fields."""
    vk_and_shift = win32api.VkKeyScan(ch)
    vk = vk_and_shift & 0xFF
    shift_state = (vk_and_shift >> 8) & 0xFF
    needs_shift = bool(shift_state & 1)
    scan = win32api.MapVirtualKey(vk, 0)

    if needs_shift:
        win32api.keybd_event(win32con.VK_SHIFT, win32api.MapVirtualKey(win32con.VK_SHIFT, 0), 0, 0)
    try:
        win32api.keybd_event(vk, scan, 0, 0)
        win32api.keybd_event(vk, scan, win32con.KEYEVENTF_KEYUP, 0)
    finally:
        if needs_shift:
            win32api.keybd_event(win32con.VK_SHIFT, win32api.MapVirtualKey(win32con.VK_SHIFT, 0), win32con.KEYEVENTF_KEYUP, 0)
    await asyncio.sleep(0.02)
