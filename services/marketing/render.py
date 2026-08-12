"""render - stage 4 of the Ghost Typer reels pipeline (PROMPTS.md A19): hands
off to the existing Remotion project (Ghosttyper-web/video) for rendering,
via a local subprocess - both repos live on this same gaming PC (confirmed
by checking, not assumed), so "hand off" means an npx call, not a network
hop, and "rendering stays on the gaming PC" holds trivially.

Composition ids used here (generated-*) are the ones registered as an
additive block in Ghosttyper-web/video/src/Root.tsx - see that block's own
comment for exactly what was added and why it's safely removable on its own
(that repo already had real uncommitted work in Root.tsx/scripts.ts before
this pipeline touched it, confirmed via `git diff` - relying on a blanket
`git checkout .` there would have discarded that pending work too, so the
addition is a clearly delimited block instead, not a git-revert promise).
"""

import asyncio
import json
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from services.marketing.format_assign import FormatAssignment

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

# (longest_text_frame, payoff_frame) per format - real per-component scene/
# reveal boundaries, confirmed by reading each format's actual Sequence/
# interpolate frame ranges in Ghosttyper-web/video/src/{GhostReel.tsx,
# formats/*.tsx}, not guessed fractions of total duration:
#   ghostreel:      SampleScene (aiText) spans frames 75-195, mid=135;
#                   ResultScene (the score payoff) spans 315-420, mid=365.
#   detector-scan:  text is calmly visible 70-170 (between scan1 and the
#                   humanize re-scan) -> 100; final reveal springs from
#                   frame 340 -> settled ~380.
#   split-screen:   both halves settled by frame 110 (bottomIn ends there)
#                   -> 150; endAt = duration-90 = 330 -> settled ~350.
#   giant-stat:     genuinely "no card, no text sample" per PLAN.md's own
#                   format table - the "longest text" frame doesn't apply
#                   here, frame 30 (topLabel visible) stands in as the
#                   second check; the score drop springs from frame 150,
#                   settled ~180.
#   grade-stamp:    the essay page settles almost immediately (pageIn
#                   springs from frame 2) -> 100 while stamps are calm;
#                   approveAt=210 (the "PASSES AS HUMAN" payoff) -> ~250.
FRAME_MARKERS = {
    "ghostreel": (135, 365),
    "detector-scan": (100, 380),
    "split-screen": (150, 350),
    "giant-stat": (30, 180),
    "grade-stamp": (100, 250),
}


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f).get("marketing", {})


def _video_dir(config: dict) -> Path:
    # Where the Remotion project actually lives - real source code in a
    # separate repo, only ever used as the subprocess cwd (node_modules/the
    # bundle resolve from here). Generated artifacts do NOT live under this
    # - see _props_dir()/_out_dir() below.
    return Path(config["ghosttyper_web_dir"]) / "video"


def _props_dir(config: dict) -> Path:
    # Relative to cortana's own ROOT, not _video_dir() - props JSON is
    # cortana's own working state, same as marketing_store/'s pattern, not
    # something that belongs inside the product repo Remotion lives in.
    return ROOT / config.get("props_subdir", "marketing_out/props")


def _out_dir(config: dict) -> Path:
    # Same reasoning as _props_dir() - rendered MP4s are cortana's output,
    # not Ghosttyper-web's source, even though the render command that
    # produces them runs with that repo as its cwd.
    return ROOT / config.get("render_out_dir", "marketing_out")


@dataclass
class RenderResult:
    video_id: str
    mp4_path: Path
    ok: bool
    stderr: str


async def _run(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    # argv list, never a joined shell string - same discipline as
    # tools/shell.py and tools/_computer_cli.py. On Windows, npm installs
    # npx as npx.cmd (a shim), and create_subprocess_exec's underlying
    # CreateProcess call does not do the PATH/extension resolution a real
    # shell would - resolving via shutil.which() first (which does search
    # PATHEXT, same as a shell) fixes this without reintroducing a shell
    # string. Confirmed live: the bare "npx" argv0 raised FileNotFoundError
    # (WinError 2) even though `npx` works fine typed directly into a shell.
    resolved = shutil.which(cmd[0]) or cmd[0]
    proc = await asyncio.create_subprocess_exec(
        resolved, *cmd[1:], cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _write_props(config: dict, video_id: str, props: dict) -> Path:
    props_dir = _props_dir(config)
    props_dir.mkdir(parents=True, exist_ok=True)
    props_path = props_dir / f"{video_id}.json"
    props_path.write_text(json.dumps(props))
    return props_path


async def render_video(assignment: FormatAssignment, video_id: str) -> RenderResult:
    config = _load_config()
    video_dir = _video_dir(config)
    props_path = _write_props(config, video_id, assignment.props)

    out_dir = _out_dir(config)
    out_dir.mkdir(parents=True, exist_ok=True)
    mp4_path = out_dir / f"{video_id}.mp4"

    cmd = ["npx", "remotion", "render", "src/index.ts", assignment.composition_id, str(mp4_path), f"--props={props_path}"]
    code, _, stderr = await _run(cmd, video_dir)
    return RenderResult(video_id=video_id, mp4_path=mp4_path, ok=(code == 0 and mp4_path.exists()), stderr=stderr)


async def render_still(assignment: FormatAssignment, video_id: str, frame: int, suffix: str) -> Path:
    """Renders one still at a specific frame - used for verify_still.py's
    payoff-frame and longest-text-frame checks (PROMPTS.md's explicit
    still-verification spec). Fast - catches overflow/logo/layout problems
    before spending a full render on a bad script/format combo."""
    config = _load_config()
    video_dir = _video_dir(config)
    props_path = _write_props(config, video_id, assignment.props)  # idempotent - same content each call for a given video_id

    still_path = _props_dir(config) / f"{video_id}-{suffix}.png"
    cmd = ["npx", "remotion", "still", "src/index.ts", assignment.composition_id, str(still_path), f"--frame={frame}", f"--props={props_path}"]
    code, _, stderr = await _run(cmd, video_dir)
    if code != 0 or not still_path.exists():
        raise RuntimeError(f"still render failed for {video_id!r} frame {frame}: {stderr}")
    return still_path


async def render_verification_stills(assignment: FormatAssignment, video_id: str) -> tuple[Path, Path]:
    """Returns (longest_text_still, payoff_still) using FRAME_MARKERS' real,
    source-confirmed frame numbers for this format."""
    longest_text_frame, payoff_frame = FRAME_MARKERS[assignment.format_name]
    longest_text_path = await render_still(assignment, video_id, longest_text_frame, "longest-text")
    payoff_path = await render_still(assignment, video_id, payoff_frame, "payoff")
    return longest_text_path, payoff_path
