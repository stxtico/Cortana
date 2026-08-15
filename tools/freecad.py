"""freecad (PROMPTS.md A26) - sends Python to an already-running FreeCAD
GUI instance's console via a small XML-RPC bridge (tools/_freecad.py
client, scripts/freecad_rpc_bootstrap.py server - see that file's docstring
for the transport decision and what it can't do). Dormant until both
FreeCAD is installed AND that bootstrap script has been pasted into its
Python console for the current session - is_available() checks the second
condition directly (a real ping, not just "is FreeCAD.exe running"); see
capability_list for the exact setup steps when this is dormant.

action='render_part' is the primary, common case: load an already-
generated, already-verified part (tools/cad.py / A14) into the live
FreeCAD document via FreeCAD's own built-in STEP importer (Part.insert()),
reusing tools/_cad_export_common.py's lookup - the exact same code
export_step.py already uses, not a second implementation. Deliberately
NOT regenerated through FreeCAD's own Part/Sketcher API: that would be a
second geometry-generation path for the same part, which could diverge
from what verify_solid() actually measured. "Same solid, same checks"
(explicit instruction) means the CadQuery-built, already-verified solid is
what appears in FreeCAD - a display bridge, not a second modeler. No
confirmation required - loading an already-verified file into a FreeCAD
document is no more consequential than export_step.py's own unconfirmed
write to a STEP file.

action='run_python' sends arbitrary Python straight through - the general
capability PROMPTS.md's own phrasing describes, for anything render_part's
fixed recipe doesn't cover. REQUIRES_CONFIRMATION-gated like tools/shell.py:
arbitrary code execution inside a live GUI process is real write-adjacent
risk, not a narrow, specific action like render_part.

Vision is untouched by this tool - PROMPTS.md's own instruction ("vision
stays last resort, always confirmed") is a reaffirmation that A14's
existing cad_generate vision check is unchanged, not a new check added
here. This tool has no vision step of its own.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from services.brain import agent_safety
from tools import _cad_export_common, _freecad

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "logs" / "freecad.jsonl"

REQUIRES_CONFIRMATION = False  # conditional - see module docstring, same pattern as tools/computer.py


def _log(record: dict) -> None:
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}) + "\n")


async def is_available() -> bool:
    return await _freecad.is_available()


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "freecad",
            "description": (
                "Send Python to an already-running FreeCAD GUI instance so geometry renders "
                "live where it can be rotated, inspected, and edited by hand. "
                "action='render_part' loads an already-generated part (from cad_generate) into "
                "FreeCAD via its own STEP importer - the exact same geometry already verified, "
                "not regenerated. action='run_python' sends arbitrary Python to FreeCAD's "
                "console directly - requires confirmation first. Requires a FreeCAD instance "
                "already running with the cortana RPC bridge started in it (dormant otherwise - "
                "see capability_list for the one-time setup)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["render_part", "run_python"]},
                    "name": {"type": "string", "description": "For action='render_part': the part's name (its directory under cad/generated/ or cad/verified/)."},
                    "code": {"type": "string", "description": "For action='run_python': the Python code to execute inside FreeCAD's console."},
                },
                "required": ["action"],
            },
        },
    }


def describe(action: str, name: str | None = None, code: str | None = None) -> str:
    if action == "run_python":
        preview = code if len(code or "") <= 200 else (code or "")[:200] + "…"
        return f"Run this Python inside the live FreeCAD instance:\n    {preview!r}"
    return f"Load {name!r} into the live FreeCAD instance."


def _render_snippet(step_path: Path, doc_label: str) -> str:
    """Generates real Python source as a string, sent verbatim through the
    RPC bridge - repr() on every embedded string value (path, label) rather
    than manual quoting/escaping, the correct way to safely embed a Python
    string literal inside generated Python source (handles Windows
    backslashes, quotes, anything else correctly, where naive
    string-replace tricks wouldn't). FreeCAD.getDocument() raising on a
    missing document (standard FreeCAD API behavior) is used to decide
    reuse-vs-create rather than depending on the exact return shape of
    listDocuments(), which this session couldn't verify live (FreeCAD isn't
    installed on this machine - see module docstring).

    Deliberately does NOT `import FreeCAD, Part` - found live, via this
    session's own stand-in-server test: scripts/freecad_rpc_bootstrap.py's
    run_code() already exec()s every sent snippet against a namespace
    pre-seeded with FreeCAD/FreeCADGui/Part (its own module docstring's
    _namespace dict), so bare names already resolve correctly without any
    import statement. Adding one anyway would make the sent snippet depend
    on those being separately importable from wherever exec() runs, on top
    of already being pre-seeded - a second, redundant contract instead of
    trusting the one the bridge already documents and guarantees."""
    return (
        "try:\n"
        f"    doc = FreeCAD.getDocument({doc_label!r})\n"
        "except Exception:\n"
        f"    doc = FreeCAD.newDocument({doc_label!r})\n"
        f"Part.insert({str(step_path)!r}, doc.Name)\n"
        "doc.recompute()\n"
        "FreeCADGui.ActiveDocument = FreeCADGui.getDocument(doc.Name)\n"
        "FreeCADGui.activeView().viewAxonometric()\n"
        "FreeCADGui.SendMsgToActiveView('ViewFit')\n"
    )


async def execute(action: str, name: str | None = None, code: str | None = None) -> str:
    if action == "run_python":
        if not code:
            return "Error: 'code' is required for action='run_python'."
        if not await agent_safety.confirm(describe(action, name, code)):
            _log({"action": "run_python", "result": "declined"})
            return "Declined by user - not executed."
        result = _freecad.run_code(code)
        _log({"action": "run_python", "result": "ok" if result.get("ok") else "error", "error": result.get("error", "")})
        if result.get("ok"):
            return f"Executed in FreeCAD. Output: {result.get('stdout') or '(none)'}"
        return f"FreeCAD reported an error:\n{result.get('error')}"

    if action == "render_part":
        if not name:
            return "Error: 'name' is required for action='render_part'."
        try:
            script_path = _cad_export_common.find_part_script(name)
        except FileNotFoundError as exc:
            return str(exc)

        step_path = script_path.parent / f"{name}.step"
        if not step_path.exists():
            result = _cad_export_common.build_default_solid(script_path)
            result.val().exportStep(str(step_path))

        snippet = _render_snippet(step_path, f"cortana_{name}")
        rpc_result = _freecad.run_code(snippet)
        _log({"action": "render_part", "name": name, "result": "ok" if rpc_result.get("ok") else "error", "error": rpc_result.get("error", "")})
        if rpc_result.get("ok"):
            return f"Loaded {name!r} ({step_path}) into the live FreeCAD instance."
        return f"FreeCAD reported an error loading {name!r}:\n{rpc_result.get('error')}"

    return f"Error: unknown action {action!r}."
