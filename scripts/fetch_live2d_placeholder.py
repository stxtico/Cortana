"""Fetches the free Live2D placeholder rig (PROMPTS.md A15) into
ui/assets/live2d/ - a one-time, reproducible setup step, not a manual
download. Not run automatically (network + a specific third-party source),
and its output is deliberately gitignored (ui/assets/live2d/) - same reason
voice_source/ is gitignored: third-party sample content this project uses
locally but doesn't redistribute through its own git history.

Model: "Shizuku," Live2D's own free Cubism 2.1 SDK sample character -
mirrored in guansss/pixi-live2d-display's test suite (MIT-licensed project,
this file is exactly the kind of demo/sample use that mirror exists for).
Full rig: idle/interaction motions, 4 expressions, physics, pose data,
textures - a genuinely complete placeholder, not a single static image.
Voiced sound effects (test/assets/shizuku/sounds/) are skipped - the
project's own TTS is the voice, not Shizuku's bundled clips.

Cubism Core runtime (the proprietary engine pixi-live2d-display needs
alongside any model - see its README): Cubism 2.1's live2d.min.js, mirrored
on jsdelivr from dylanNew/live2d since Live2D stopped hosting it directly
after 2019/9/4.

    uv run scripts/fetch_live2d_placeholder.py
"""

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE2D_DIR = ROOT / "ui" / "assets" / "live2d"

_RAW_BASE = "https://raw.githubusercontent.com/guansss/pixi-live2d-display/master/test/assets/shizuku"

_MODEL_FILES = [
    "shizuku.moc",
    "shizuku.model.json",
    "shizuku.physics.json",
    "shizuku.pose.json",
    *[f"expressions/f0{i}.exp.json" for i in range(1, 5)],
    *[f"motions/{name}_{n:02d}.mtn" for name in ("flickHead", "idle", "pinchIn", "pinchOut", "shake", "tapBody") for n in range(3)],
    *[f"shizuku.1024/texture_0{i}.png" for i in range(6)],
]

_CORE_URL = "https://cdn.jsdelivr.net/gh/dylanNew/live2d/webgl/Live2D/lib/live2d.min.js"


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=30) as resp:
        dest.write_bytes(resp.read())


def main() -> None:
    print(f"Fetching Shizuku (Cubism 2.1 placeholder rig) into {LIVE2D_DIR.relative_to(ROOT)}...")
    for rel in _MODEL_FILES:
        dest = LIVE2D_DIR / "shizuku" / rel
        if dest.exists():
            continue
        _download(f"{_RAW_BASE}/{rel}", dest)
        print(f"  {rel}")

    core_dest = LIVE2D_DIR / "core" / "live2d.min.js"
    if not core_dest.exists():
        _download(_CORE_URL, core_dest)
        print("  core/live2d.min.js")

    total = sum(1 for _ in LIVE2D_DIR.rglob("*") if _.is_file())
    print(f"Done - {total} files in {LIVE2D_DIR.relative_to(ROOT)}.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Fetch failed: {exc}", file=sys.stderr)
        sys.exit(1)
