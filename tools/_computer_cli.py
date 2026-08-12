"""CLI-recipe target resolution (PROMPTS.md A18) - the third resolution tier:
for apps where the fastest, most reliable path isn't the GUI at all (PLAN.md's
own framing), declared per app in [tools.computer.apps.<name>].open_command,
e.g. explorer.exe opening a folder directly rather than simulating clicks
through Explorer's own UI.

Same never-build-a-shell-command-line-string discipline as tools/shell.py:
argv stays a real list the whole way down (asyncio.create_subprocess_exec),
so a path or argument value is inert data, never shell syntax. Unlike
shell.py this isn't sandboxed in WSL - it runs directly on the Windows host -
but a recipe can only exist for an app already named in
[tools.computer.apps], the same per-application allowlist gate every other
resolution tier goes through (tools/computer.py's config loading is the one
place recipes get read from; nothing here accepts an arbitrary command).
"""

import asyncio


async def run_recipe(command_template: list[str], **placeholders: str) -> tuple[int, str, str]:
    """Substitutes {name}-style placeholders into each argv element
    individually (never into a joined string) then runs it directly. No
    target-resolution step needed - the whole point of this tier is skipping
    the GUI, not clicking through it."""
    argv = [part.format(**placeholders) for part in command_template]
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )
