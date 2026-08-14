"""Post-action verification (PROMPTS.md A22 Step 3) - tools/computer.py calls
snapshot() before, and compare() after, every click/type. A resolved target
and an executed click are not proof the intended state actually changed:
this project's own A14 lesson (gemma3:12b fabricated a wrong CAD-render
verdict with total confidence) generalizes here - a wrong UIA element or a
wrong grounder guess resolves and clicks just as confidently as a right one,
and nothing before this module would have caught the difference.

Two independent signals, chosen by how the target was resolved:
- uia/uia_setofmark: re-query the accessibility tree for the same element by
  name after the action. Exact and cheap, but only possible when the target
  came from the tree in the first place.
- vision (last resort, no tree entry to re-check): screenshot-diff a small
  region around the click point instead.

Neither signal is a pass/fail oracle - PROMPTS.md A22 Step 3's own framing is
"turn silent failures into caught ones," not "auto-decide success." An
element that still resolves identically after a click might mean the click
had no effect, or might mean it had an effect somewhere else in the window;
a changed screenshot region might mean the intended thing happened, or might
mean an unrelated animation was mid-flight. tools/computer.py never retries
on a mismatch - it reports the verification detail in what's returned to the
caller and lets a human or the calling agent decide, per this session's own
explicit instruction against retrying blind.
"""

from dataclasses import dataclass

from PIL import Image, ImageChops, ImageGrab, ImageStat

from tools import _computer_uia

UIA_TIERS = {"uia", "uia_setofmark"}


@dataclass
class VerifyResult:
    tier: str  # "uia" or "screenshot" - the verification mechanism used, logged alongside resolved_via (the resolution tier) so the two are never conflated
    outcome: str  # "changed" | "unchanged" | "vanished"
    detail: str


def _snapshot_uia(process_match: str, name: str):
    element = _computer_uia.resolve(process_match, name=name)
    if element is None:
        return None
    return (element.control_type, element.left, element.top, element.right, element.bottom)


def _snapshot_region(x: int, y: int, radius: int) -> Image.Image:
    return ImageGrab.grab(bbox=(x - radius, y - radius, x + radius, y + radius)).convert("RGB")


def snapshot(resolved_via: str, process_match: str, resolved_name: str, x: int, y: int, radius: int):
    """Called immediately before the click/type, so the "before" state is
    genuinely pre-action, not reconstructed after the fact."""
    if resolved_via in UIA_TIERS:
        return ("uia", _snapshot_uia(process_match, resolved_name))
    return ("screenshot", _snapshot_region(x, y, radius))


def compare(before, process_match: str, resolved_name: str, x: int, y: int, radius: int, diff_threshold: float) -> VerifyResult:
    """Called after the action (and a short settle delay, left to the
    caller) - re-snapshots the same signal captured by snapshot() and
    reports what changed, without judging whether that change was the
    intended one."""
    tier, before_data = before
    if tier == "uia":
        after_data = _snapshot_uia(process_match, resolved_name)
        if before_data is not None and after_data is None:
            return VerifyResult(tier, "vanished", f"{resolved_name!r} no longer resolves via the accessibility tree after the action - it may have closed, navigated away, or been consumed by the click.")
        if before_data == after_data:
            return VerifyResult(tier, "unchanged", f"{resolved_name!r} resolves identically (same control type and rectangle) before and after the action - no UIA-visible state change detected.")
        return VerifyResult(tier, "changed", f"{resolved_name!r}'s accessibility-tree state (control type or rectangle) changed after the action.")

    after_img = _snapshot_region(x, y, radius)
    before_img = before_data
    if before_img.size != after_img.size:
        return VerifyResult(tier, "changed", f"Screenshot region around ({x}, {y}) changed size after the action.")
    diff = ImageChops.difference(before_img, after_img)
    mean_diff = sum(ImageStat.Stat(diff).mean) / 3.0
    if mean_diff >= diff_threshold:
        return VerifyResult(tier, "changed", f"Screenshot region around ({x}, {y}) changed visibly after the action (mean pixel diff {mean_diff:.1f} >= threshold {diff_threshold}).")
    return VerifyResult(tier, "unchanged", f"Screenshot region around ({x}, {y}) did not change visibly after the action (mean pixel diff {mean_diff:.1f} < threshold {diff_threshold}).")
