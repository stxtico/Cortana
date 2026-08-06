"""Agent loop (PROMPTS.md A8): tool list -> model picks -> execute -> feed result
back -> repeat. Hand-rolled, not LangChain - a while-loop over messages with a
dispatch dict, per PLAN.md's own reasoning (adds more abstraction than it
removes at this scale) and A6's precedent of avoiding heavy frameworks for
memory. services/brain/client.py already passes tools through in OpenAI format
and yields a tool_calls chunk as a JSON string (A1, smoke-tested) - this module
is the only new part; the model side already holds (e4b was validated against
this exact workload - 6 selection cases plus a real multi-step chain - before
the switch from 12b, see CLAUDE.md).

Tool-call iterations from Ollama don't stream meaningful prose alongside the
tool_calls array in practice (confirmed A1) - so tokens are yielded to the
caller as they arrive, same as services/brain/client.py's plain streaming, and
only the final iteration (no tool_calls) ever produces a real spoken answer.
If a future model ever does stream substantial commentary before invoking a
tool, that commentary would currently be spoken even though more processing
follows - a known, accepted edge case, not solved here.

web_search, shell, calendar_read, and email_read are all registered like any
other tool but gated live via an is_available() check (same pattern for all
four - see _drop_unavailable_tools() below): no Docker/dedicated WSL distro
means shell can't run, no Outlook means calendar/email can't, no search
backend means web_search can't. Each offers itself to the model automatically
the moment its real dependency exists, no code change needed - see CLAUDE.md's
A8/A9 entries for what's actually deferred and why.

write_file and shell are write-capable and gated a second, different way:
REQUIRES_CONFIRMATION (checked in _call_tool) blocks execution until
agent_safety.confirm() returns True - real dispatcher code, not a persona
instruction (CLAUDE.md rule 4, and A5a's padding investigation found negative
persona constraints hold at roughly two-thirds reliability - nowhere near a
safety bar). agent_safety.py also enforces the no-credentials rule on every
tool call's arguments, and owns the global abort hotkey.

ask_user (A10) gets the same "don't trust the prompt" treatment for its one
numeric constraint: persona.md's one-question-per-turn policy is real
guidance for *when* asking is right, but the cap itself is enforced here in
run_agent() by counting real ask_user calls this turn, not left to the model
to self-limit.
"""

import asyncio
import json
import time
import tomllib
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from services.brain import agent_safety
from services.brain import client as brain_client
from tools import ask_user, calendar_read, email_read, fetch_url, list_dir, read_file, shell, web_search, write_file

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"
AGENT_LOG_PATH = ROOT / "logs" / "agent.jsonl"

MAX_ITERATIONS = 10

# Prepended/merged into the system message when tools are offered. Found
# necessary by live testing, not added speculatively: without it, e4b
# sometimes narrated its plan in prose ("I will read the config directory to
# locate it") instead of actually emitting a tool call, or skipped the tool
# entirely and guessed a plausible-sounding number - both silent failure
# modes a generic tool schema alone didn't prevent.
_TOOL_USE_INSTRUCTION = (
    "Tools are available below. If answering requires information you don't "
    "already know for certain - a file's contents, a directory listing, a web "
    "search result - call the appropriate tool. Don't guess, and don't just "
    "describe what you're about to do in words; actually call it."
)

# Every agent-callable tool, name -> module (each exposes spec() + async execute()).
_ALL_TOOLS = {
    "web_search": web_search,
    "fetch_url": fetch_url,
    "read_file": read_file,
    "list_dir": list_dir,
    "write_file": write_file,
    "shell": shell,
    "calendar_read": calendar_read,
    "email_read": email_read,
    "ask_user": ask_user,
}


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def _tools_for_model(model: str, config: dict) -> dict:
    """Tool dispatch keyed on active model (A8 requirement) - config-driven, not
    hardcoded, so a future heavy/alt model can get a different set without
    touching this loop. No [tools.by_model] entry for a model means "all
    tools" - today's only real caller, [models].primary, gets the full set,
    read and write-capable alike. The confirmation gate and no-credentials
    rule in _call_tool() are what actually bound what a write-capable tool can
    do, not tool-list membership - a future model with tighter permissions
    would be scoped here instead."""
    allowed = config.get("tools", {}).get("by_model", {}).get(model)
    if allowed is None:
        return dict(_ALL_TOOLS)
    return {name: _ALL_TOOLS[name] for name in allowed if name in _ALL_TOOLS}


def _log(record: dict) -> None:
    AGENT_LOG_PATH.parent.mkdir(exist_ok=True)
    with AGENT_LOG_PATH.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}) + "\n")


async def _drop_unavailable_tools(tools: dict) -> dict:
    """Live-checks every tool exposing is_available() (web_search, shell,
    calendar_read, email_read) and drops it from the offered set if it
    returns False - offering a tool that can only fail wastes a turn on a
    guaranteed error instead of the model just not seeing it. Checked fresh
    every run_agent() call, not cached, so each starts working automatically
    the moment its real dependency exists.

    CLAUDE.md rule 10: every is_available() called here runs unconditionally
    on every turn, for every gated tool - it must be cheap, read-only, and
    incapable of starting or changing anything. tools/_outlook.py's first
    version violated this (Dispatch() launches Outlook if not already
    running) and was caught live, not in review - see that module and
    CLAUDE.md's A9 entry for what it actually did on this machine."""
    available = {}
    for name, tool in tools.items():
        check = getattr(tool, "is_available", None)
        if check is None:
            available[name] = tool
            continue
        if await check():
            available[name] = tool
        else:
            _log({"stage": "tool_unavailable", "tool": name})
    return available


async def _call_tool(name: str, arguments: dict, tools: dict, cap: int) -> str:
    """Runs one tool call and logs it - arguments and result only, never
    anything a tool module might hold internally (e.g. tools/_search_tavily.py's
    API key never enters `arguments` or `result`, since it's not part of the
    tool's schema or return value - see that module's docstring).

    Two dispatcher-enforced gates happen here, before any tool code runs, both
    real code rather than persona instructions (see agent_safety.py): the
    no-credentials rule (every tool call's arguments are scanned), and the
    confirmation gate for anything a tool module flags with
    REQUIRES_CONFIRMATION = True. Execution itself is wrapped in a task
    registered with agent_safety so the global abort hotkey can cancel it
    mid-flight."""
    tool = tools.get(name)
    start = time.perf_counter()

    if tool is None:
        result, ok = f"Error: no tool named {name!r} is available.", False
    elif (violation := agent_safety.credential_violation(arguments)) is not None:
        result, ok = f"Refused: {violation}.", False
        _log({"stage": "credential_refused", "tool": name, "reason": violation})
    elif getattr(tool, "REQUIRES_CONFIRMATION", False) and not await agent_safety.confirm(
        tool.describe(**arguments) if hasattr(tool, "describe") else f"{name}({arguments})"
    ):
        result, ok = "Declined by user - not executed.", False
    else:
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(tool.execute(**arguments))
        agent_safety.register_current_task(task, loop)
        try:
            result, ok = await task, True
        except asyncio.CancelledError:
            result, ok = "Aborted by user (hotkey) - execution stopped mid-way.", False
        except Exception as exc:
            result, ok = f"Error running {name}: {exc}", False
        finally:
            agent_safety.clear_current_task()

    original_chars = len(result)
    truncated = original_chars > cap
    if truncated:
        result = result[:cap] + f"\n...[truncated, {original_chars} chars total]"

    _log({
        "stage": "tool_call",
        "tool": name,
        "arguments": arguments,
        "ok": ok,
        "result_chars": len(result),
        "original_chars": original_chars,
        "truncated": truncated,
        "duration_ms": round((time.perf_counter() - start) * 1000, 1),
    })
    return result


async def run_agent(messages: list[dict], model: str | None = None) -> AsyncIterator[str]:
    """Runs the tool-use loop for one turn. `messages` should already include
    the system prompt + conversation history + the new user turn - same shape
    services/brain/loop.py builds via MemoryManager.build_messages(). Yields
    assistant text tokens as they arrive on the final (non-tool-call) response,
    same streaming contract as services/brain/client.py.stream()."""
    config = _load_config()
    active_model = model or config["models"]["primary"]
    tools = _tools_for_model(active_model, config)
    tools = await _drop_unavailable_tools(tools)
    specs = [t.spec() for t in tools.values()]
    cap = config.get("tools", {}).get("max_result_chars", 3000)
    think = config.get("thinking", {}).get("agent", True)

    abort_cfg = config.get("tools", {}).get("abort_hotkey", {})
    if abort_cfg.get("enabled", True):
        installed = agent_safety.install_abort_hotkey(abort_cfg.get("hotkey", "ctrl+shift+x"))
        if not installed:
            _log({"stage": "abort_hotkey", "outcome": "not_installed"})

    ask_user_max = config.get("tools", {}).get("ask_user_max_per_turn", 1)
    ask_user_count = 0

    messages = list(messages)
    if specs:
        if messages and messages[0].get("role") == "system":
            messages[0] = {**messages[0], "content": messages[0]["content"] + "\n\n" + _TOOL_USE_INSTRUCTION}
        else:
            messages.insert(0, {"role": "system", "content": _TOOL_USE_INSTRUCTION})
    for _ in range(MAX_ITERATIONS):
        assistant_chunks: list[str] = []
        tool_calls: list[dict] = []

        async for token in brain_client.stream(messages, tools=specs, think=think):
            parsed = None
            try:
                parsed = json.loads(token)
            except json.JSONDecodeError:
                pass
            if isinstance(parsed, dict) and "tool_calls" in parsed:
                tool_calls.extend(parsed["tool_calls"])
                continue
            assistant_chunks.append(token)
            yield token

        if not tool_calls:
            return  # a real answer, not another tool round - done

        messages.append({
            "role": "assistant",
            "content": "".join(assistant_chunks),
            "tool_calls": tool_calls,
        })

        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments", {})
            arguments = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")

            if name == "ask_user":
                # Real dispatcher-enforced cap (A10) - persona.md's "one
                # clarifying question per turn" is guidance for *when* asking
                # is right, not something the model is trusted to self-limit.
                ask_user_count += 1
                if ask_user_count > ask_user_max:
                    result = "Already asked a clarifying question this turn - proceed with your best judgment instead of asking again."
                    _log({"stage": "ask_user_cap", "outcome": "blocked", "count": ask_user_count})
                    messages.append({"role": "tool", "name": name, "content": result})
                    continue

            result = await _call_tool(name, arguments, tools, cap)
            messages.append({"role": "tool", "name": name, "content": result})

    _log({"stage": "iteration_cap_hit", "max_iterations": MAX_ITERATIONS})
    yield f"\n(Hit the {MAX_ITERATIONS}-iteration tool-use cap without a final answer.)"


async def _main() -> None:
    import sys

    query = " ".join(sys.argv[1:]) or "Read the config file and tell me what the wake threshold is."
    print(f"> {query}\n")
    messages = [{"role": "user", "content": query}]
    async for token in run_agent(messages):
        print(token, end="", flush=True)
    print(f"\n\n(logged to {AGENT_LOG_PATH})")
    await brain_client.aclose()


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
