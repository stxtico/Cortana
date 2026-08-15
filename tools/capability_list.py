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
"""

REQUIRES_CONFIRMATION = False

_DORMANT_REASONS = {
    "web_search": "no search backend is currently reachable ([tools.web_search].backend in config/cortana.toml, and either a Tavily API key or a live SearXNG endpoint)",
    "shell": "the isolated CortanaShell WSL2 distro doesn't exist on this machine yet, or its automount isn't disabled (see CLAUDE.md's A9 entry)",
    "calendar_read": "Outlook isn't currently running - this reads via a live COM connection to an already-running Outlook, never launching it itself (CLAUDE.md rule 10)",
    "email_read": "Outlook isn't currently running - same reason as calendar_read",
}
_DEFAULT_DORMANT_REASON = "a dependency this tool needs isn't currently available"


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
    return "\n\n".join(sections)
