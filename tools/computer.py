"""computer (PROMPTS.md A18/A22) - GUI automation: resolves a target through
the accessibility tree first (tools/_computer_uia.py, Windows UI Automation),
then Playwright for browsers (tools/_computer_playwright.py, dormant until a
debug-port browser exists), then a CLI recipe where one is configured
(tools/_computer_cli.py), and only as a last resort, a purpose-built
grounding model + coordinates (tools/_computer_vision.py, [models].
vision_grounding = gta1-7b as of A22 - see below). This ordering isn't a
preference against vision as a category - A22 found that was too broad a
conclusion from A14's finding, which was really about gemma3:12b (a
general-purpose VLM) being the wrong tool for click-coordinate grounding
specifically, not that vision-based grounding is inherently unreliable.
GTA1-7B, a model trained via RL specifically to output grounded click
coordinates (not verbose reasoning), measured 81.8% accuracy on a real
33-target benchmark built from this machine's own Explorer/VS Code/Chrome/
Terminal/cortana's-own-UI (100% easy / 68.4% hard - see docs/history/A22.md)
- a different class of result than gemma3:12b's fabrication/false-negative
failures. UIA still goes first regardless, not because the grounder can't be
trusted at all, but because UIA is *exact* where it resolves (A22 also fixed
a real bug in _computer_uia.resolve() that had silently zeroed out UIA
coverage on VS Code/Chrome/Electron apps - the true UIA baseline is 75.8%,
not the 39.4% first measured) - every target UIA recovers is certain, not
probabilistic, so it stays authoritative ahead of any model. The grounder is
still real last resort, not upgraded to co-equal: 81.8% isn't good enough to
act on unconfirmed, which is why vision-resolved clicks still always require
confirmation below, unconditionally - that hasn't changed with the model
swap, only the specific numbers behind why.

Every click/type goes through the real performance layer (walk -> working
state -> eased cursor move -> pause -> click), never an instant teleport -
see tools/_computer_input.py's docstring for why that pacing is a safety
feature, not decoration, and services/character/walk_signal.py for how the
walk phase reaches the Electron character window. The kill switch this
depends on (services/brain/agent_safety.py's abort hotkey) was built and
live-verified - mechanism, a real physical keypress, and focus-independence
across a different focused app - before a single line of this file existed.

REQUIRES_CONFIRMATION is False at module level (services/brain/agent.py's
dispatcher only reads that flag statically, and the instruction was
confirmation for anything that sends/deletes/purchases/submits, not every
click). Instead execute() calls agent_safety.confirm() directly and
conditionally - still the same real, dispatcher-adjacent gate the model can't
route around (A9's confirm()), just invoked from inside this module rather
than unconditionally by agent.py itself. Vision-resolved clicks always
confirm regardless of action type, a deliberate addition beyond the four
named categories - the target itself is unreliably identified there, not
just the action.

Per-application allowlist ([tools.computer.apps]) is enforced twice: the
`app` parameter's JSON-schema enum means the model structurally cannot name
an app that isn't configured (constrain the input's shape, not filter its
content - the same reasoning tools/shell.py's command whitelist and A14's
tools/_cad_units.py's unit-tagged dimensions both apply), and the actual
foreground process is re-checked live immediately before every click/type -
not cached from when the target was resolved, in case a different app
grabbed focus in between.

Typing is only permitted when the target was resolved via UI Automation
(uia or uia_setofmark - see below), where CurrentIsPassword is a real,
structural property to check - a deliberate, narrower scope than "detect
passwords everywhere," same "constrain shape" reasoning applied to which
resolution tiers even permit keystroke synthesis, not just what gets refused
after the fact. A password-flagged field refuses unconditionally, no
confirmation offered - the instruction was "never," not "confirm first."

A22 Step 2 adds a fifth resolved_via value, uia_setofmark
(tools/_computer_setofmark.py): reached only when UIA's exact-name lookup
misses AND tools/_computer_uia.py's find_candidates() finds the miss was
genuinely ambiguous (loose name matches exist), never run alongside a clean
UIA hit - deliberately narrower than always cross-validating every action,
since A22 Step 1's own overlap analysis found that added latency for no
measured benefit (21 of 25 targets where both tiers fired were the grounder
merely agreeing with an already-exact UIA answer). It draws numbered boxes
over the real UIA rectangles of every candidate and asks a vision model to
pick one - location is UIA-exact, only "which one" is a guess, which is why
it still requires confirmation unconditionally, same as raw vision.

A22 Step 3 adds post-action verification (tools/_computer_verify.py): a
snapshot taken immediately before every click/type, compared against a
fresh one after (a UIA re-query for uia/uia_setofmark targets, a screenshot
diff of the click region otherwise). Every outcome is logged alongside the
resolution tier that produced the target, specifically so a pattern of
UIA-resolved actions failing their post-check would actually be visible
later (a wrong UIA element still resolves and clicks with full confidence -
nothing before this existed to catch that). Neither signal is a pass/fail
oracle, and a mismatch never triggers an automatic retry - the verification
detail is appended to what execute() returns so a human or the calling
agent decides what to do next, not this module guessing.
"""

import asyncio
import json
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from services.brain import agent_safety
from services.character import walk_signal
from tools import (
    _computer_cli,
    _computer_input,
    _computer_playwright,
    _computer_setofmark,
    _computer_uia,
    _computer_verify,
    _computer_vision,
)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"
LOG_PATH = ROOT / "logs" / "computer.jsonl"

REQUIRES_CONFIRMATION = False  # confirmed conditionally inside execute() - see module docstring

_DEFAULT_CONFIRM_TRIGGERS = ["send", "delete", "submit", "buy", "purchase", "confirm order", "pay"]


@dataclass
class _Target:
    x: int
    y: int
    name: str
    resolved_via: str
    is_password: bool = False
    hwnd: int | None = None  # only set for uia-resolved targets - what the focus step below actually foregrounds


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def _computer_config() -> dict:
    return _load_config().get("tools", {}).get("computer", {})


def _log(record: dict) -> None:
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}) + "\n")


def _progress(stage: str) -> None:
    """Real-time stage visibility to stdout - same "print, don't just log"
    precedent as agent_safety.confirm()'s "[CONFIRMATION NEEDED]" line. Added
    specifically so a human running this live can see which stage is
    in-flight and choose the moment to press the abort hotkey (most
    meaningfully during "moving cursor" - that's the whole point of the
    performance layer, per PLAN.md's own framing), not a test-only add - this
    is useful the same way in real conversational use."""
    print(f"[computer] {stage}")


def spec() -> dict:
    config = _computer_config()
    apps = list(config.get("apps", {}).keys())
    return {
        "type": "function",
        "function": {
            "name": "computer",
            "description": (
                "Control the mouse/keyboard to interact with a real desktop application. "
                f"Only these allowlisted apps: {', '.join(apps) if apps else '(none configured)'}. "
                "action='open' launches the app directly (e.g. a folder or file path) rather than "
                "clicking through its UI - the fastest, most reliable path when a direct command "
                "exists. action='click'/'double_click' resolves a real on-screen control by name/"
                "description (the accessibility tree first, never guessed pixels unless every other "
                "resolution method fails) and clicks it, with a visible walk-and-cursor-move "
                "performance step that can be aborted mid-motion. action='type' types text into "
                "whatever was just resolved by target - refuses outright on a password field, and "
                "only works when target was resolved through the accessibility tree in the first "
                "place. Sending, deleting, purchasing, or submitting anything requires spoken "
                "confirmation first, and so does anything resolved by last-resort vision guessing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {"type": "string", "enum": apps, "description": "Which allowlisted application to act on."},
                    "action": {"type": "string", "enum": ["open", "click", "double_click", "type"]},
                    "path": {"type": "string", "description": "For action='open': the path/argument to open (e.g. a folder path)."},
                    "target": {"type": "string", "description": "For click/double_click/type: a plain description of the on-screen element (e.g. 'the bracket file', 'the Submit button')."},
                    "text": {"type": "string", "description": "For action='type': the text to type. Never a password - that request is refused."},
                },
                "required": ["app", "action"],
            },
        },
    }


def describe(app: str, action: str, path: str | None = None, target: str | None = None, text: str | None = None) -> str:
    """Code-generated confirmation text, not model-phrased - same reasoning
    as tools/write_file.py's describe(): the safety-relevant text can't
    depend on the model getting the phrasing right."""
    if action == "open":
        return f"Open {path!r} in {app}."
    if action == "type":
        return f"Type {text!r} into {target or 'the currently resolved field'} in {app}."
    return f"{action.replace('_', ' ').title()} on {target!r} in {app}."


def _matches_trigger(text: str, triggers: list[str]) -> bool:
    lowered = text.lower()
    return any(t.lower() in lowered for t in triggers)


async def _resolve(app_cfg: dict, target: str) -> _Target | None:
    """Runs the resolution tiers in priority order, returns the first hit or
    None if every tier misses - a miss is an ordinary outcome (returned to
    the model as "couldn't find it"), never an exception.

    Step 2 (A22) arbitration is deliberately narrow, not an always-run-both
    design: it only fires when UIA's own exact-name lookup misses AND
    find_candidates() finds the miss was genuinely ambiguous (one or more
    loose name matches), never alongside a clean UIA hit. A22 Step 1's own
    overlap analysis found cross-validating every action would have added
    latency for no benefit - 25/33 benchmark targets had both UIA and the
    grounder fire, and of those, 21 were the grounder merely agreeing with an
    already-exact answer and the other 4 were the grounder being wrong."""
    computer_cfg = _computer_config()
    process_match = app_cfg.get("match_process", "")
    resolve_hwnd: int | None = None

    if process_match:
        uia_result = _computer_uia.resolve(process_match, name=target)
        if uia_result is not None:
            return _Target(uia_result.center_x, uia_result.center_y, uia_result.name, "uia", uia_result.is_password, uia_result.hwnd)

        threshold = computer_cfg.get("fuzzy_match_threshold", 0.6)
        candidates = _computer_uia.find_candidates(process_match, target, threshold=threshold)
        if candidates:
            resolve_hwnd = candidates[0].hwnd
            description_model = _load_config().get("models", {}).get("vision", "")
            if description_model:
                som_result = await _computer_setofmark.resolve(description_model, target, candidates, hwnd=resolve_hwnd)
                if som_result is not None:
                    return _Target(som_result.center_x, som_result.center_y, som_result.name, "uia_setofmark", som_result.is_password, som_result.hwnd)
        else:
            # Genuinely nothing UIA can offer (not even a loose match) - a
            # window handle is still useful below for cropping the vision
            # grounder's screenshot, even though no UIA *element* was found
            # (this is exactly cortana's own control panel case from A22
            # Step 1: zero UIA elements, but the window itself is still a
            # real, enumerable top-level window).
            top_hwnds = _computer_uia.find_top_level_hwnds(process_match)
            resolve_hwnd = top_hwnds[0] if top_hwnds else None

    playwright_cfg = app_cfg.get("playwright", {})
    if playwright_cfg:
        port = playwright_cfg.get("cdp_port", 9222)
        if await _computer_playwright.is_available(port):
            selector = playwright_cfg.get("selector_template", "text={target}").format(target=target)
            pw_result = await _computer_playwright.resolve(port, selector)
            if pw_result is not None:
                return _Target(pw_result.center_x, pw_result.center_y, pw_result.name, "playwright")

    models_cfg = _load_config().get("models", {})
    grounding_model = models_cfg.get("vision_grounding", "")
    description_model = models_cfg.get("vision", "")
    if grounding_model and description_model:
        vision_result = await _computer_vision.resolve(grounding_model, description_model, target, hwnd=resolve_hwnd)
        if vision_result is not None:
            x, y, what_you_see = vision_result
            return _Target(x, y, what_you_see, "vision")

    return None


async def _perform_click(x: int, y: int, double: bool) -> None:
    """The real performance layer: walk request -> wait for arrival -> eased
    cursor move -> brief pause -> click -> reset to idle. Every await point
    here is a real point services/brain/agent_safety.py's abort hotkey can
    land on (asyncio.Task.cancel() only interrupts at an await, per
    tools/_computer_input.py's own docstring) - this function makes no
    attempt to swallow CancelledError, so an abort mid-sequence propagates
    straight out to services/brain/agent.py's _call_tool(), which reports it
    as "Aborted by user (hotkey)"."""
    _progress(f"walking toward x={x}...")
    request_id = walk_signal.request_walk(x)
    arrived = await walk_signal.wait_for_arrival(request_id, timeout_s=5.0)
    _progress("arrived, working" if arrived else "walk signal timed out (character window not running?) - proceeding anyway")
    try:
        _progress(f"moving cursor to ({x}, {y})...")
        await _computer_input.move_cursor_eased(x, y, duration_s=0.5)
        _progress("clicking")
        await _computer_input.click("left")
        if double:
            await _computer_input.click("left")
    finally:
        walk_signal.signal_idle()


async def execute(app: str, action: str, path: str | None = None, target: str | None = None, text: str | None = None) -> str:
    config = _computer_config()
    apps_cfg = config.get("apps", {})
    app_cfg = apps_cfg.get(app)
    if app_cfg is None:
        return f"Error: {app!r} is not an allowlisted application."

    confirm_triggers = config.get("confirm_triggers", _DEFAULT_CONFIRM_TRIGGERS)

    if action == "open":
        open_command = app_cfg.get("open_command")
        if not open_command:
            return f"Error: {app!r} has no configured open_command."
        # Real bug, found live: a model-guessed path that doesn't exist (e.g.
        # a hallucinated "bracket part" instead of a real search hit) used to
        # be reported as a false success, because the exit-code-is-
        # informational-only handling below (needed for explorer.exe's own
        # quirk) had no accompanying check that path itself was real. Verify
        # before declaring done (CLAUDE.md rule 6) applies to a tool's own
        # return value, not just human verification - so path is checked to
        # actually exist before ever invoking open_command on it.
        if path:
            resolved_path = Path(path)
            if not resolved_path.is_absolute():
                resolved_path = ROOT / resolved_path
            if not resolved_path.exists():
                _log({"stage": "action", "app": app, "action": "open", "path": path, "resolved_via": "cli", "result": "not_found"})
                return f"Error: {path!r} doesn't exist - not opening it. Search with list_dir/read_file for the real location first."
        # explorer.exe (and several other GUI-launching Windows commands)
        # routinely exit non-zero even on real success - it hands the request
        # off to the already-running shell process and its own launcher exit
        # code isn't a meaningful success signal. Confirmed live: `explorer.exe
        # config` opens the folder correctly but returns exit 1 every time.
        # Treated as informational, not pass/fail, for this action type.
        code, stdout, stderr = await _computer_cli.run_recipe(open_command, path=path or "")
        _log({"stage": "action", "app": app, "action": "open", "path": path, "resolved_via": "cli", "result": "ok", "exit_code": code})
        return f"Opened {path!r} in {app}."

    if target is None:
        return "Error: 'target' is required for click/double_click/type."

    _progress(f"resolving {target!r} in {app} (accessibility tree first)...")
    resolved = await _resolve(app_cfg, target)
    if resolved is None:
        _log({"stage": "action", "app": app, "action": action, "target": target, "resolved_via": None, "result": "not_found"})
        return f"Couldn't find {target!r} in {app} through any resolution method."
    _progress(f"resolved via {resolved.resolved_via}: {resolved.name!r} at ({resolved.x}, {resolved.y})")

    # Establish focus herself rather than depending on the user having
    # already clicked into the target app - real autonomous use ("open the
    # bracket file") can't assume that. Best-effort only: SetForegroundWindow
    # from a background process is unreliable by Windows' own design
    # (confirmed empirically - see tools/_computer_uia.focus_window()'s
    # docstring for the real numbers, not assumed). The safety property is
    # unchanged either way - the live re-check immediately below still runs
    # and still refuses exactly as before if this didn't land or landed on
    # the wrong process; this step only changes whether she has to depend on
    # someone else establishing that state first.
    if resolved.hwnd is not None:
        _computer_uia.focus_window(resolved.hwnd)
        await asyncio.sleep(0.15)  # a brief real wait for the OS focus change to actually land before re-checking

    # Live re-check, immediately before synthesizing anything - not trusted
    # from when the target was resolved, in case a different window grabbed
    # focus in between (tools/computer.py's own docstring / this session's
    # allowlist discussion).
    foreground = _computer_uia.foreground_process_name()
    if app_cfg.get("match_process", "").lower() not in foreground.lower():
        _log({"stage": "action", "app": app, "action": action, "target": target, "resolved_via": resolved.resolved_via, "result": "refused_wrong_foreground", "foreground": foreground})
        return f"Refused: {app} is not the foreground application right now ({foreground} is) - not clicking blind."

    if action == "type":
        if resolved.resolved_via not in _computer_verify.UIA_TIERS:
            _log({"stage": "action", "app": app, "action": "type", "target": target, "resolved_via": resolved.resolved_via, "result": "refused_unverifiable"})
            return "Refused: typing is only supported for a target resolved through the accessibility tree, where a password field can actually be verified."
        if resolved.is_password:
            _log({"stage": "action", "app": app, "action": "type", "target": target, "resolved_via": resolved.resolved_via, "result": "refused_password"})
            return "Refused: that field is a password field. Never - type it yourself."

    # Set-of-mark clicks are UIA-exact on location but a guess on *which*
    # candidate was meant - same reasoning as vision, still requires
    # confirmation unconditionally, not just on a trigger word.
    needs_confirm = resolved.resolved_via in ("vision", "uia_setofmark") or (text and _matches_trigger(text, confirm_triggers)) or _matches_trigger(resolved.name, confirm_triggers)
    if needs_confirm:
        prompt = describe(app, action, path, target, text)
        if resolved.resolved_via == "vision":
            prompt += f"\n(Resolved via last-resort vision guessing, not the accessibility tree - it believes it's looking at: {resolved.name!r}. Double-check before approving.)"
        elif resolved.resolved_via == "uia_setofmark":
            prompt += f"\n(The accessibility tree had multiple loose matches for {target!r}; a vision model picked {resolved.name!r} as the best match among real candidates. Double-check before approving.)"
        if not await agent_safety.confirm(prompt):
            _log({"stage": "action", "app": app, "action": action, "target": target, "resolved_via": resolved.resolved_via, "result": "declined"})
            return "Declined by user - not executed."

    verify_cfg = config.get("verify", {})
    verify_radius = verify_cfg.get("diff_radius", 60)
    verify_settle_s = verify_cfg.get("settle_delay_s", 0.4)
    verify_diff_threshold = verify_cfg.get("diff_threshold", 8.0)
    before_snapshot = _computer_verify.snapshot(resolved.resolved_via, app_cfg.get("match_process", ""), resolved.name, resolved.x, resolved.y, verify_radius)

    if action in ("click", "double_click"):
        await _perform_click(resolved.x, resolved.y, double=(action == "double_click"))
        await asyncio.sleep(verify_settle_s)
        verify_result = _computer_verify.compare(before_snapshot, app_cfg.get("match_process", ""), resolved.name, resolved.x, resolved.y, verify_radius, verify_diff_threshold)
        _log({"stage": "action", "app": app, "action": action, "target": target, "resolved_via": resolved.resolved_via, "resolved_name": resolved.name, "x": resolved.x, "y": resolved.y, "result": "ok", "verify_tier": verify_result.tier, "verify_outcome": verify_result.outcome})
        return f"{action.replace('_', ' ').title()}ed on {resolved.name!r} in {app} (resolved via {resolved.resolved_via}). Verification ({verify_result.tier}): {verify_result.detail}"

    if action == "type":
        await _perform_click(resolved.x, resolved.y, double=False)  # focus the field first, same performance layer
        for ch in text or "":
            await _computer_input.type_char(ch)
        await asyncio.sleep(verify_settle_s)
        verify_result = _computer_verify.compare(before_snapshot, app_cfg.get("match_process", ""), resolved.name, resolved.x, resolved.y, verify_radius, verify_diff_threshold)
        _log({"stage": "action", "app": app, "action": "type", "target": target, "resolved_via": resolved.resolved_via, "result": "ok", "verify_tier": verify_result.tier, "verify_outcome": verify_result.outcome})
        return f"Typed into {resolved.name!r} in {app}. Verification ({verify_result.tier}): {verify_result.detail}"

    return f"Error: unknown action {action!r}."
