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

web_search is registered like any other tool but gated live (see the
is_available() check below) - no Docker on this machine and no Tavily key
means neither search backend actually works right now, a deliberate
infrastructure deferral, not a broken build (see CLAUDE.md). It'll offer
itself to the model automatically the moment either backend becomes real,
no code change needed.
"""

import json
import time
import tomllib
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from services.brain import client as brain_client
from tools import fetch_url, list_dir, read_file, web_search

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
}


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def _tools_for_model(model: str, config: dict) -> dict:
    """Tool dispatch keyed on active model (A8 requirement) - config-driven, not
    hardcoded, so a future heavy/alt model can get a different set without
    touching this loop. No [tools.by_model] entry for a model means "all
    tools" - today's only real caller, [models].primary, gets the full
    read-only set. This is the seam A9's write tools (confirmation-gated) hook
    into later without restructuring anything here."""
    allowed = config.get("tools", {}).get("by_model", {}).get(model)
    if allowed is None:
        return dict(_ALL_TOOLS)
    return {name: _ALL_TOOLS[name] for name in allowed if name in _ALL_TOOLS}


def _log(record: dict) -> None:
    AGENT_LOG_PATH.parent.mkdir(exist_ok=True)
    with AGENT_LOG_PATH.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}) + "\n")


async def _call_tool(name: str, arguments: dict, tools: dict, cap: int) -> str:
    """Runs one tool call and logs it - arguments and result only, never
    anything a tool module might hold internally (e.g. tools/_search_tavily.py's
    API key never enters `arguments` or `result`, since it's not part of the
    tool's schema or return value - see that module's docstring)."""
    tool = tools.get(name)
    start = time.perf_counter()
    if tool is None:
        result = f"Error: no tool named {name!r} is available."
        ok = False
    else:
        try:
            result = await tool.execute(**arguments)
            ok = True
        except Exception as exc:
            result = f"Error running {name}: {exc}"
            ok = False

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
    if "web_search" in tools and not await web_search.is_available():
        # No live SearXNG instance and no Tavily key (PROMPTS.md A8, deferred
        # pending real search infrastructure - see CLAUDE.md) - offering a tool
        # that can only fail wastes a turn on a guaranteed error instead of
        # just not being there.
        del tools["web_search"]
        _log({"stage": "web_search_unavailable", "backend": config.get("tools", {}).get("web_search", {}).get("backend")})
    specs = [t.spec() for t in tools.values()]
    cap = config.get("tools", {}).get("max_result_chars", 3000)
    think = config.get("thinking", {}).get("agent", True)

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
