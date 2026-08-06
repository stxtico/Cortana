"""shell (PROMPTS.md A9) - runs a whitelisted command inside a dedicated,
isolated WSL2 distro, never on the Windows host directly. PLAN.md is explicit:
never give an autonomous loop unrestricted shell on the host - this isn't that.

Isolation is filesystem-enforced, not application-level filtering: the
configured distro ([tools.shell].distro) has automount disabled in its
/etc/wsl.conf, so nothing is ever mounted at /mnt/c inside it - there's
nothing for a command to reach even if it tried, the same guarantee a
container's filesystem namespace would give, not a blocklist that only has to
have one gap. is_available() verifies this live, every call, not just once at
setup, by checking the real mount table for a drvfs entry (see its own
docstring - two prior versions inferred this from /mnt's directory contents
instead and both were wrong in persistent ways). See CLAUDE.md's A9 entry for
the exact `wsl --import` + wsl.conf setup steps (run by the user, not
provisioned here).

Commands run via `wsl.exe -d <distro> -e <command> <args...>` - the `-e` flag
executes directly without invoking the default Linux shell, and both the
Windows-side subprocess call and this direct-argv path never build a single
shell command-line string, so argument values are inert data to the target
program, never shell syntax - the whitelist and this together are the actual
defense, not just the isolation boundary.
"""

import asyncio
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

REQUIRES_CONFIRMATION = True


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("tools", {}).get("shell", {})


def spec() -> dict:
    config = _load_config()
    whitelist = config.get("whitelist", [])
    return {
        "type": "function",
        "function": {
            "name": "shell",
            "description": (
                "Run one command inside an isolated Linux sandbox (no access to any "
                "Windows files or the rest of the host). Only these exact commands are "
                f"allowed: {', '.join(whitelist) if whitelist else '(none configured)'}. "
                "Requires user confirmation before it actually runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command name, exactly as whitelisted - no path, no shell syntax."},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Arguments to pass to the command, as separate list items.",
                    },
                },
                "required": ["command"],
            },
        },
    }


async def _run_wsl(distro: str, argv: list[str], cwd: str | None = None, timeout_s: float = 15.0) -> tuple[int, str, str]:
    """Runs argv directly (no shell) inside `distro` via wsl.exe -e - argv stays
    a real argument list the whole way down (asyncio subprocess, then wsl.exe's
    -e), never a single command-line string, so nothing in it is ever
    interpreted as shell syntax."""
    cmd = ["wsl.exe", "-d", distro]
    if cwd:
        cmd += ["--cd", cwd]
    cmd += ["-e", *argv]
    # stdin=DEVNULL, not left to default (inherit) - found live, the hard way
    # (CLAUDE.md's A10 entry): an inherited stdin handle let this subprocess
    # silently consume/close piped input meant for tools/ask_user.py's later
    # input() call, surfacing as an EOFError with no obvious connection to
    # this module at all.
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"timed out after {timeout_s}s"
    return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")


async def is_available() -> bool:
    """Live, every call - not a one-time-at-startup latch (same reasoning as
    tools/_search_searxng.py's check): the distro must exist, and no Windows
    drive can be mounted inside it right now, not assumed from the fact
    setup was done at some point in the past.

    Checks the real mount table (`mount`) for a `drvfs` entry - not directory
    contents under /mnt. Two prior versions of this check both inferred
    mount state from `/mnt`'s directory listing and both were wrong in ways
    that could persist indefinitely: checking whether `/mnt` itself was empty
    fails forever, because WSL always has `/mnt/wsl` and `/mnt/wslg` there -
    its own tmpfs internals, unrelated to Windows drives, present regardless
    of the automount setting. A follow-up version narrowed to checking
    `/mnt/c` specifically for emptiness, which was closer, but still an
    inference - it assumes an empty `/mnt/c` means unmounted rather than
    checking that directly, and breaks if `/mnt/c` gets removed entirely
    (confirmed live: it was, as an empty leftover from the distro's first
    boot). `mount`'s own output is the actual kernel-level ground truth, not
    an inference from what a directory happens to contain - `drvfs` is the
    filesystem type WSL uses for every Windows-drive mount, so its total
    absence from `mount` is a direct answer, not a proxy for one."""
    config = _load_config()
    distro = config.get("distro", "")
    if not distro:
        return False
    code, stdout, _ = await _run_wsl(distro, ["mount"], timeout_s=5.0)
    if code != 0:
        return False  # distro doesn't exist, or wsl.exe itself failed
    return "drvfs" not in stdout.lower()


def describe(command: str, args: list[str] | None = None) -> str:
    args = args or []
    return f"Run `{command} {' '.join(args)}` inside the isolated shell sandbox (no access to Windows files)."


async def execute(command: str, args: list[str] | None = None) -> str:
    config = _load_config()
    whitelist = config.get("whitelist", [])
    if command not in whitelist:
        return f"Error: {command!r} is not in the configured shell whitelist ({', '.join(whitelist)})."

    distro = config.get("distro", "")
    if not distro:
        return "Error: no [tools.shell].distro configured."

    args = args or []
    code, stdout, stderr = await _run_wsl(
        distro, [command, *args],
        cwd=config.get("sandbox_cwd", "/root"),
        timeout_s=config.get("timeout_s", 15.0),
    )
    output = stdout
    if stderr:
        output += f"\n[stderr]\n{stderr}"
    return f"(exit {code})\n{output}" if output.strip() else f"(exit {code}, no output)"
