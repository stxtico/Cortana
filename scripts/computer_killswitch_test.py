"""Live verification for tools/computer.py's kill switch (PROMPTS.md A18) -
built and run BEFORE any target-resolution or click code exists, per explicit
instruction ("build the abort before you build the click"). Not a mock: this
moves the real OS cursor using the exact primitive (tools/_computer_input.py's
move_cursor_eased()) computer.py's execute() will actually call, registered
through the exact dispatcher mechanism (services/brain/agent_safety.py) A9's
abort hotkey already uses for every other tool - no second kill-switch system,
same reasoning as agent.py's own task-cancellation wiring.

Runs unattended: the hotkey press is synthesized via raw win32 keybd_event
calls (with real scan codes - see _press_combo below), not typed by a human
and NOT via the `keyboard` package's own send(). Found live, the hard way:
keyboard.send() sets an internal _listener.is_replaying flag while it runs,
and separately (confirmed by reading keyboard/_winkeyboard.py's low-level
hook directly) a first attempt at raw keybd_event() injection still didn't
fire the hotkey - passing scan_code=0 to keybd_event left the hook's
scan-code-to-name lookup unable to resolve "ctrl"/"shift"/"x", so the
combo never matched even though real KEYDOWN/KEYUP events reached the OS.
Fixed by resolving each key's real scan code via win32api.MapVirtualKey()
before injecting - confirmed firing in isolation before wiring it into this
script. This is a real synthetic key event indistinguishable to the OS's
low-level hook from a physical press once the scan code is correct, so this
proves the actual cross-thread cancellation path: hook fires -> call_soon_
threadsafe(task.cancel) -> CancelledError raised inside the real cooperative
move loop -> cursor stops. A9's own abort-hotkey verification left the
physical-keypress case as a "test the first time someone's actually at the
keyboard" follow-up; nothing here removes the value of that real test too,
but it isn't a substitute for verifying the mechanism itself works, which
this script does directly and repeatably.

    uv run scripts/computer_killswitch_test.py            # automated, synthesized press
    uv run scripts/computer_killswitch_test.py --manual    # waits for a real physical press instead

--manual runs the identical two-sweep structure with no synthesized press at
all - sweep 1 waits on whatever key event actually reaches the real installed
hotkey, sweep 2 still checks for stuck state afterward. This is the case the
automated run above can't cover on its own: the scan-code investigation above
showed synthetic and physical key events aren't equivalent by default in this
stack (a naive synthetic press silently did nothing until the scan code was
fixed), which is exactly why a genuine physical press is still worth
confirming separately, not assumed equivalent just because the automated
mechanism now passes.
"""

import asyncio
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(ROOT))

import win32api
import win32con

from services.brain import agent_safety
from tools import _computer_input

CONFIG_PATH = ROOT / "config" / "cortana.toml"

_VK_BY_NAME = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "x": 0x58}


def _press_combo(hotkey: str) -> None:
    """Presses then releases every key in a '+'-joined combo string (e.g.
    'ctrl+shift+x') via raw win32 keybd_event, real scan codes resolved via
    MapVirtualKey - see this module's docstring for why that resolution step
    is load-bearing, not decorative. Keys release in reverse order, matching
    how a person actually releases a held combo."""
    vks = [_VK_BY_NAME[name.strip().lower()] for name in hotkey.split("+")]
    for vk in vks:
        win32api.keybd_event(vk, win32api.MapVirtualKey(vk, 0), 0, 0)
        time.sleep(0.02)
    for vk in reversed(vks):
        win32api.keybd_event(vk, win32api.MapVirtualKey(vk, 0), win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.02)

SWEEP_DURATION_S = 4.0  # deliberately long relative to a real click's ~0.5s move - gives a wide, unambiguous window to confirm the abort landed well before natural completion
MANUAL_SWEEP_DURATION_S = 8.0  # longer than the automated sweep - a real person needs time to notice the moving cursor and react, not just a fixed offset
ABORT_AFTER_S = 1.0  # when sweep 1's synthetic hotkey press fires (automated mode only)


def _load_hotkey() -> str:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("tools", {}).get("abort_hotkey", {}).get("hotkey", "ctrl+shift+x")


async def _sweep(duration_s: float) -> None:
    """One long, slow, visible cursor sweep - real target-resolution code will
    never move this slowly (duration_s is 8-16x the real click pacing),
    stretched out on purpose so a human has time to see it and press the
    hotkey mid-motion, not because this is representative of production timing."""
    left = win32api.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    top = win32api.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
    width = win32api.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    height = win32api.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    win32api.SetCursorPos((left + 20, top + height // 2))
    await asyncio.sleep(0.3)
    await _computer_input.move_cursor_eased(left + width - 20, top + height // 2, duration_s=duration_s)


async def _run_one_sweep(label: str, *, duration_s: float, press_hotkey_after_s: float | None, hotkey: str) -> tuple[str, float]:
    """Runs one sweep wired through the exact same registration the real
    dispatcher uses (services/brain/agent.py's _call_tool()) - register, await,
    catch CancelledError, clear - so this test exercises the real cancellation
    path, not a simplified stand-in for it. If press_hotkey_after_s is set,
    schedules a synthesized keypress at that offset (automated mode); if None,
    nothing presses the hotkey from inside this process at all - the only way
    sweep completes early is a real key event from outside it (--manual mode)."""
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(_sweep(duration_s))
    agent_safety.register_current_task(task, loop)

    press_task = None
    if press_hotkey_after_s is not None:
        async def _press_later():
            await asyncio.sleep(press_hotkey_after_s)
            _press_combo(hotkey)
        press_task = asyncio.ensure_future(_press_later())

    start = time.perf_counter()
    try:
        await task
        elapsed = time.perf_counter() - start
        print(f"[{label}] completed normally in {elapsed:.2f}s")
        return "completed", elapsed
    except asyncio.CancelledError:
        elapsed = time.perf_counter() - start
        pos = _computer_input.cursor_position()
        print(f"[{label}] CANCELLED after {elapsed:.2f}s - cursor stopped at {pos}")
        return "cancelled", elapsed
    finally:
        agent_safety.clear_current_task()
        if press_task is not None:
            press_task.cancel()


async def _main_automated(hotkey: str) -> None:
    print(f"\nSweep 1: cursor moves left->right over {SWEEP_DURATION_S:.1f}s; hotkey fires (synthesized) at {ABORT_AFTER_S:.1f}s.")
    outcome_1, elapsed_1 = await _run_one_sweep("sweep 1", duration_s=SWEEP_DURATION_S, press_hotkey_after_s=ABORT_AFTER_S, hotkey=hotkey)

    await asyncio.sleep(0.5)  # let the hook thread settle before the next sweep

    print(f"\nSweep 2: same motion, no hotkey press - checking the cancellation above left no stuck state.")
    outcome_2, elapsed_2 = await _run_one_sweep("sweep 2", duration_s=SWEEP_DURATION_S, press_hotkey_after_s=None, hotkey=hotkey)

    print("\n--- Result ---")
    landed_promptly = outcome_1 == "cancelled" and elapsed_1 < (ABORT_AFTER_S + 0.5)
    completed_cleanly = outcome_2 == "completed" and elapsed_2 > (SWEEP_DURATION_S - 0.5)
    print(f"Sweep 1: {outcome_1} at {elapsed_1:.2f}s (hotkey fired at {ABORT_AFTER_S:.1f}s, sweep would naturally finish at {SWEEP_DURATION_S:.1f}s) - {'PASS' if landed_promptly else 'FAIL'}")
    print(f"Sweep 2: {outcome_2} at {elapsed_2:.2f}s (expected ~{SWEEP_DURATION_S:.1f}s) - {'PASS' if completed_cleanly else 'FAIL'}")
    if landed_promptly and completed_cleanly:
        print("\nPASS: kill switch stopped real cursor motion mid-flight (well before natural completion), and left no stuck state behind.")
    else:
        print("\nFAIL: re-check before writing any click code (see this script's docstring).")


async def _main_manual(hotkey: str) -> None:
    print(f"\nSweep 1: cursor will move slowly left->right over {MANUAL_SWEEP_DURATION_S:.0f}s.")
    print(f"Press {hotkey} for real, any time during that window, to test the actual kill switch.")
    input("Press Enter when you're ready to start sweep 1... ")
    outcome_1, elapsed_1 = await _run_one_sweep("sweep 1", duration_s=MANUAL_SWEEP_DURATION_S, press_hotkey_after_s=None, hotkey=hotkey)

    await asyncio.sleep(0.5)

    print(f"\nSweep 2: same motion, do NOT press the hotkey this time.")
    print("Checking that the real cancellation above didn't leave anything stuck.")
    input("Press Enter when you're ready to start sweep 2... ")
    outcome_2, elapsed_2 = await _run_one_sweep("sweep 2", duration_s=MANUAL_SWEEP_DURATION_S, press_hotkey_after_s=None, hotkey=hotkey)

    print("\n--- Result ---")
    print(f"Sweep 1 (expected 'cancelled' if you pressed the hotkey): {outcome_1} at {elapsed_1:.2f}s")
    print(f"Sweep 2 (expected 'completed' at ~{MANUAL_SWEEP_DURATION_S:.0f}s): {outcome_2} at {elapsed_2:.2f}s")
    if outcome_1 == "cancelled" and outcome_2 == "completed":
        print("\nPASS: a real physical keypress stopped real cursor motion mid-flight, and left no stuck state behind.")
    else:
        print("\nNot a clean pass - re-check manually before writing any click code (see this script's docstring).")


async def main() -> None:
    manual = "--manual" in sys.argv[1:]
    hotkey = _load_hotkey()
    installed = agent_safety.install_abort_hotkey(hotkey)
    if not installed:
        print(f"FAILED to install abort hotkey ({hotkey!r}) - see logs/agent.jsonl for the error. Stopping.")
        return
    print(f"Abort hotkey installed: {hotkey!r}")

    if manual:
        await _main_manual(hotkey)
    else:
        await _main_automated(hotkey)


if __name__ == "__main__":
    asyncio.run(main())
