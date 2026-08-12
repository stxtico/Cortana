"""computer (PROMPTS.md A18) - GUI automation: resolves a target through the
accessibility tree first (tools/_computer_uia.py, Windows UI Automation),
then Playwright for browsers (tools/_computer_playwright.py, dormant until a
debug-port browser exists), then a CLI recipe where one is configured
(tools/_computer_cli.py), and only as an absolute last resort, vision +
coordinates (tools/_computer_vision.py). This ordering isn't a preference -
A14's CLAUDE.md entry found the vision model this project uses fabricates
specific wrong claims about an image rather than admitting uncertainty, and
separately produced a real false negative even in an already-hardened setup.
Pixel-coordinate clicking driven by a model that invents details is the worst
combination available here, so it only ever runs when every other tier misses.

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

Typing is only permitted when the target was resolved via UI Automation,
where CurrentIsPassword is a real, structural property to check - a
deliberate, narrower scope than "detect passwords everywhere," same
"constrain shape" reasoning applied to which resolution tiers even permit
keystroke synthesis, not just what gets refused after the fact. A
password-flagged field refuses unconditionally, no confirmation offered -
the instruction was "never," not "confirm first."
"""

import json
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from services.brain import agent_safety
from services.character import walk_signal
from tools import _computer_cli, _computer_input, _computer_playwright, _computer_uia, _computer_vision

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


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def _computer_config() -> dict:
    return _load_config().get("tools", {}).get("computer", {})


def _log(record: dict) -> None:
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}) + "\n")


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
    the model as "couldn't find it"), never an exception."""
    process_match = app_cfg.get("match_process", "")
    if process_match:
        uia_result = _computer_uia.resolve(process_match, name=target)
        if uia_result is not None:
            return _Target(uia_result.center_x, uia_result.center_y, uia_result.name, "uia", uia_result.is_password)

    playwright_cfg = app_cfg.get("playwright", {})
    if playwright_cfg:
        port = playwright_cfg.get("cdp_port", 9222)
        if await _computer_playwright.is_available(port):
            selector = playwright_cfg.get("selector_template", "text={target}").format(target=target)
            pw_result = await _computer_playwright.resolve(port, selector)
            if pw_result is not None:
                return _Target(pw_result.center_x, pw_result.center_y, pw_result.name, "playwright")

    vision_model = _load_config().get("models", {}).get("vision", "")
    if vision_model:
        vision_result = await _computer_vision.resolve(vision_model, target)
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
    request_id = walk_signal.request_walk(x)
    walk_signal.wait_for_arrival(request_id, timeout_s=5.0)
    try:
        await _computer_input.move_cursor_eased(x, y, duration_s=0.5)
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

    resolved = await _resolve(app_cfg, target)
    if resolved is None:
        _log({"stage": "action", "app": app, "action": action, "target": target, "resolved_via": None, "result": "not_found"})
        return f"Couldn't find {target!r} in {app} through any resolution method."

    # Live re-check, immediately before synthesizing anything - not trusted
    # from when the target was resolved, in case a different window grabbed
    # focus in between (tools/computer.py's own docstring / this session's
    # allowlist discussion).
    foreground = _computer_uia.foreground_process_name()
    if app_cfg.get("match_process", "").lower() not in foreground.lower():
        _log({"stage": "action", "app": app, "action": action, "target": target, "resolved_via": resolved.resolved_via, "result": "refused_wrong_foreground", "foreground": foreground})
        return f"Refused: {app} is not the foreground application right now ({foreground} is) - not clicking blind."

    if action == "type":
        if resolved.resolved_via != "uia":
            _log({"stage": "action", "app": app, "action": "type", "target": target, "resolved_via": resolved.resolved_via, "result": "refused_unverifiable"})
            return "Refused: typing is only supported for a target resolved through the accessibility tree, where a password field can actually be verified."
        if resolved.is_password:
            _log({"stage": "action", "app": app, "action": "type", "target": target, "resolved_via": resolved.resolved_via, "result": "refused_password"})
            return "Refused: that field is a password field. Never - type it yourself."

    needs_confirm = resolved.resolved_via == "vision" or (text and _matches_trigger(text, confirm_triggers)) or _matches_trigger(resolved.name, confirm_triggers)
    if needs_confirm:
        prompt = describe(app, action, path, target, text)
        if resolved.resolved_via == "vision":
            prompt += f"\n(Resolved via last-resort vision guessing, not the accessibility tree - it believes it's looking at: {resolved.name!r}. Double-check before approving.)"
        if not await agent_safety.confirm(prompt):
            _log({"stage": "action", "app": app, "action": action, "target": target, "resolved_via": resolved.resolved_via, "result": "declined"})
            return "Declined by user - not executed."

    if action in ("click", "double_click"):
        await _perform_click(resolved.x, resolved.y, double=(action == "double_click"))
        _log({"stage": "action", "app": app, "action": action, "target": target, "resolved_via": resolved.resolved_via, "resolved_name": resolved.name, "x": resolved.x, "y": resolved.y, "result": "ok"})
        return f"{action.replace('_', ' ').title()}ed on {resolved.name!r} in {app} (resolved via {resolved.resolved_via})."

    if action == "type":
        await _perform_click(resolved.x, resolved.y, double=False)  # focus the field first, same performance layer
        for ch in text or "":
            await _computer_input.type_char(ch)
        _log({"stage": "action", "app": app, "action": "type", "target": target, "resolved_via": resolved.resolved_via, "result": "ok"})
        return f"Typed into {resolved.name!r} in {app}."

    return f"Error: unknown action {action!r}."
