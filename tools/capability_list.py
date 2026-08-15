"""capability_list (PROMPTS.md A23) - "what can you do?" is the most
natural question anyone asks an assistant, and answering it honestly means
never presenting a dormant or gated tool as unconditionally available.
Reports three states, built from the REAL tool registry
(services/brain/agent.py's _ALL_TOOLS) and each tool's own live
is_available() check - never a hand-maintained list of tool names, which
would silently drift the first time a tool is added, removed, or its own
gating condition changes (this session's own explicit instruction: the trap
in "what can you do" is that half these tools are dormant or gated, and a
list presenting everything as available would be actively misleading).

Import of services.brain.agent is deferred to execute() rather than done at
module level - agent.py imports every tool module (including this one) at
ITS OWN module level to build _ALL_TOOLS, so importing agent.py back at
this module's load time would be a circular import. Deferring to call time
works because by the time any tool's execute() actually runs, agent.py has
already finished its own module-level execution (it's what caused this
module to be imported in the first place).

Dormant reasons are the one piece of genuinely-authored text here - a
boolean is_available() result carries no "why" on its own, and that
information doesn't exist anywhere else in the codebase as data. Kept
deliberately small and honest about its own incompleteness: a tool not
covered by _DORMANT_REASONS still gets a generic fallback rather than
silently vanishing from the dormant list or crashing - so a future gated
tool added without an entry here degrades gracefully, not silently.

PROMPTS.md A25 added scope notes for the same reason: available/gated/
dormant is a binary per tool, but "can drive one allowlisted app" and "can
drive anything except a short exclusion list" are very different answers to
"what can you do," and A25 changed computer/look_at_screen from the former
to the latter. A static description would drift the moment the exclusion
list is actually populated (still empty right now - see below), so this
reads [tools].excluded_windows live, same "never hand-maintain what's
checkable" discipline as the rest of this module.
"""

import tomllib
from pathlib import Path

REQUIRES_CONFIRMATION = False

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _ROOT / "config" / "cortana.toml"

_DORMANT_REASONS = {
    "web_search": "no search backend is currently reachable ([tools.web_search].backend in config/cortana.toml, and either a Tavily API key or a live SearXNG endpoint)",
    "shell": "the isolated CortanaShell WSL2 distro doesn't exist on this machine yet, or its automount isn't disabled (see CLAUDE.md's A9 entry)",
    "calendar_read": "Outlook isn't currently running - this reads via a live COM connection to an already-running Outlook, never launching it itself (CLAUDE.md rule 10)",
    "email_read": "Outlook isn't currently running - same reason as calendar_read",
    "email_draft": "Outlook isn't currently running - same reason as calendar_read",
    "ocr": "Tesseract isn't installed on this machine (winget install --id UB-Mannheim.TesseractOCR)",
}
_DEFAULT_DORMANT_REASON = "a dependency this tool needs isn't currently available"

_SCOPE_NOTE_TOOLS = {"computer", "look_at_screen"}


def _excluded_windows() -> list[str]:
    with _CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("tools", {}).get("excluded_windows", [])


def _scope_note(name: str, excluded: list[str]) -> str:
    """PROMPTS.md A25 - computer and look_at_screen share one exclusion list
    ([tools].excluded_windows) and the same default-allow-except shape, so
    one note format covers both; the verb differs (drive vs. read)."""
    verb = "drive any application's UI (click/type)" if name == "computer" else "read the content of any window"
    if excluded:
        return f"{name}: can {verb}, except {len(excluded)} excluded window(s): {', '.join(excluded)}."
    return (
        f"{name}: can {verb} - EXCLUSION LIST IS EMPTY. Nothing is actually walled off right now; "
        f"add password-manager/banking window titles to [tools].excluded_windows in config/cortana.toml "
        f"before treating this as a real boundary, not after."
    )


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "capability_list",
            "description": (
                "List what tools are actually usable right now, what's dormant and why, and "
                "what requires confirmation before it runs. Use this to answer 'what can you "
                "do' honestly, rather than assuming every tool that exists is currently usable."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


def _format_section(title: str, items: list[str]) -> str:
    body = "\n".join(f"  - {item}" for item in items) if items else "  (none)"
    return f"{title}\n{body}"


async def execute() -> str:
    from services.brain import agent  # deferred - see module docstring

    available, gated, dormant = [], [], []
    for name, tool in sorted(agent._ALL_TOOLS.items()):
        check = getattr(tool, "is_available", None)
        if check is not None and not await check():
            reason = _DORMANT_REASONS.get(name, _DEFAULT_DORMANT_REASON)
            dormant.append(f"{name} - {reason}")
            continue
        if getattr(tool, "REQUIRES_CONFIRMATION", False):
            gated.append(name)
            continue
        available.append(name)

    sections = [
        _format_section("Available now:", available),
        _format_section("Available, but asks for confirmation before each use:", gated),
        _format_section("Currently dormant (a real dependency is missing - not offered to you at all right now):", dormant),
    ]

    excluded = _excluded_windows()
    scope_notes = [_scope_note(name, excluded) for name in sorted(_SCOPE_NOTE_TOOLS) if name in agent._ALL_TOOLS]
    if scope_notes:
        sections.append(_format_section("Scope notes (these tools are broader than a plain 'available' entry implies):", scope_notes))

    return "\n\n".join(sections)
