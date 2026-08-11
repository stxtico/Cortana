"""Headless PNG rendering for tools/cad.py's render-compare loop (PROMPTS.md
A14 - "render PNGs from 3 angles"). cadquery.vis.show(interact=False,
screenshot=...) does real off-screen VTK rendering - confirmed by actually
looking at the output before relying on it: edges=True produced a busy
cross-hatched render (every tessellation triangle outlined) that read
worse than the plain shaded faces, so edges=False is what's used here."""

from pathlib import Path

import cadquery as cq
import cadquery.vis as cq_vis

# Three angles around the vertical axis plus a fixed elevation - enough to
# see faces/features that a single view would hide (PLAN.md: "render to PNG
# from several angles"), not an exhaustive turntable.
ANGLES = [(0, -30), (120, -30), (240, 20)]


def render_angles(result, out_dir: Path, prefix: str = "angle") -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, (azimuth, elevation) in enumerate(ANGLES):
        path = out_dir / f"{prefix}_{i}.png"
        cq_vis.show(
            result,
            interact=False,
            screenshot=str(path),
            azimuth=azimuth,
            elevation=elevation,
            edges=False,
            bgcolor=(1, 1, 1),
        )
        paths.append(path)
    return paths
