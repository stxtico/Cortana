"""Human-attended end-to-end verification of the full computer-use pipeline
(PROMPTS.md A18) - the one thing not yet exercised: the whole
resolve -> walk -> cursor -> click sequence firing for real, in the real
production code path (tools/computer.py's execute(), not a reconstructed
stand-in), with a human keeping the target app focused throughout.

Not unattended by design: the sandboxed test terminal used earlier kept
regaining OS foreground focus between subprocess calls, and the live
foreground-allowlist re-check in tools/computer.py correctly refused every
time - exactly as designed, but it means an unattended script can't complete
this particular test. That's what this script is for - it opens the target
folder, then waits for a human to click into it and keep it focused before
firing the real click sequence.

Wired through the exact same task registration services/brain/agent.py's
_call_tool() uses (register_current_task/clear_current_task around the
execute() call), so the real abort hotkey (ctrl+shift+x, or whatever
[tools.abort_hotkey].hotkey is set to) can really cancel it - press it any
time after "resolved via ..." prints to test aborting mid-sequence. Progress
prints stage-by-stage (tools/computer.py's own _progress()) so you can see
exactly where it is: resolving -> walking -> arrived/working ->
moving cursor -> clicking.

Default target is the real A13 CAD bracket part - the same folder/file
PROMPTS.md's own A18 done-when names ("open the CAD folder and pull up the
bracket"), confirmed genuinely present and committed (see CLAUDE.md's A18
entry - an earlier "cad/ doesn't exist" finding was a false alarm from
checking the wrong directory).

    uv run scripts/computer_e2e_test.py
    uv run scripts/computer_e2e_test.py --path some/other/folder --target some_file.ext
"""

import asyncio
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.brain import agent_safety
from tools import computer

CONFIG_PATH = ROOT / "config" / "cortana.toml"
DEFAULT_APP = "explorer"
DEFAULT_PATH = "cad/verified/bracket"
DEFAULT_TARGET = "part.py"


def _load_hotkey() -> str:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("tools", {}).get("abort_hotkey", {}).get("hotkey", "ctrl+shift+x")


async def _run_registered(coro, label: str) -> str:
    """Same registration the real dispatcher does around every tool call
    (services/brain/agent.py:_call_tool()) - this is what makes the abort
    hotkey able to cancel this specific call, not a simplified stand-in."""
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(coro)
    agent_safety.register_current_task(task, loop)
    try:
        return await task
    except asyncio.CancelledError:
        print(f"\n[{label}] ABORTED - kill switch fired mid-sequence.")
        return "(aborted)"
    finally:
        agent_safety.clear_current_task()


async def main() -> None:
    args = sys.argv[1:]
    path = DEFAULT_PATH
    target = DEFAULT_TARGET
    if "--path" in args:
        path = args[args.index("--path") + 1]
    if "--target" in args:
        target = args[args.index("--target") + 1]

    hotkey = _load_hotkey()
    if not agent_safety.install_abort_hotkey(hotkey):
        print(f"FAILED to install abort hotkey ({hotkey!r}) - stopping.")
        return
    print(f"Abort hotkey armed: {hotkey!r}\n")

    print(f"Step 1: opening {path!r} in {DEFAULT_APP}...")
    open_result = await _run_registered(computer.execute(app=DEFAULT_APP, action="open", path=path), "open")
    print(f"  -> {open_result}\n")

    print(f"Step 2: click into the {path!r} Explorer window now and keep it focused.")
    print(f"         (the live foreground check refuses to click blind if it isn't.)")
    input("Press Enter when it's focused and you're ready... ")

    print(f"\nStep 3: resolving and clicking {target!r} - watch the stages below.")
    print(f"        Press {hotkey} at any point to test aborting mid-sequence")
    print(f"        (most meaningfully right after 'moving cursor to ...' prints).\n")
    click_result = await _run_registered(
        computer.execute(app=DEFAULT_APP, action="double_click", target=target), "click"
    )
    print(f"\n  -> {click_result}")


if __name__ == "__main__":
    asyncio.run(main())
