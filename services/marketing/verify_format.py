"""verify_format - stage 6 of the Ghost Typer reels pipeline (PROMPTS.md
A19): ffprobe assertion that a rendered MP4 is actually Reels-valid. Same
exact command the ghost-typer-reels skill documents, run as a subprocess
(argv list, not a shell string) and parsed rather than eyeballed.

This check IS a hard gate, unlike verify_still.py's vision signal - width/
height/pixel format are exact, deterministic facts ffprobe reports, not a
fuzzy guess, so a mismatch here means the render is genuinely wrong, not a
"maybe" - fail the batch loudly (PLAN.md's own words) rather than queueing
something Reels will reject or colour-shift. yuv420p vs yuvj420p matters
specifically because the project renders png frames (not jpeg) for this
reason - see SKILL.md's own "Render" section.
"""

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FormatCheck:
    ok: bool
    width: int | None
    height: int | None
    pix_fmt: str | None
    error: str


async def check_format(mp4_path: Path) -> FormatCheck:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return FormatCheck(ok=False, width=None, height=None, pix_fmt=None, error="ffprobe not found on PATH.")

    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v",
        "-show_entries", "stream=width,height,pix_fmt", "-of", "csv=p=0", str(mp4_path),
    ]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return FormatCheck(ok=False, width=None, height=None, pix_fmt=None, error=stderr.decode(errors="replace").strip())

    line = stdout.decode(errors="replace").strip()
    parts = line.split(",")
    if len(parts) != 3:
        return FormatCheck(ok=False, width=None, height=None, pix_fmt=None, error=f"unexpected ffprobe output: {line!r}")

    width, height, pix_fmt = parts
    try:
        width_i, height_i = int(width), int(height)
    except ValueError:
        return FormatCheck(ok=False, width=None, height=None, pix_fmt=pix_fmt, error=f"unexpected ffprobe output: {line!r}")

    ok = width_i == 1080 and height_i == 1920 and pix_fmt == "yuv420p"
    error = "" if ok else f"expected 1080x1920 yuv420p, got {width_i}x{height_i} {pix_fmt}"
    return FormatCheck(ok=ok, width=width_i, height=height_i, pix_fmt=pix_fmt, error=error)
