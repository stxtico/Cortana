"""Thin async Ollama client. Streams tokens, logs TTFT/duration per call.

No agent loop, no tool dispatch here - that's A8. `tools` is forwarded to Ollama
in OpenAI function-calling format so a future caller can pass it through; a
tool_calls response is yielded as a JSON string since the caller isn't set up
to consume anything richer yet.
"""

import json
import time
import tomllib
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"
BRAIN_LOG_PATH = ROOT / "logs" / "brain.jsonl"

_client: httpx.AsyncClient | None = None
_last_stats: dict | None = None


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    models = config["models"]
    if not models["primary"]:
        raise SystemExit(f"[models].primary is empty in {CONFIG_PATH}")
    return models


def _get_client(endpoint: str) -> httpx.AsyncClient:
    # Reused across calls - a new httpx.AsyncClient per call cost ~280ms in
    # connection setup alone, a quarter of the whole latency budget.
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=endpoint, timeout=120.0)
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def last_call_stats() -> dict | None:
    """Ollama's server-side stats (prompt_eval_count, etc.) from the most recent
    stream() call. Doesn't change stream()'s AsyncIterator[str] contract - added
    for A6's rolling-context trigger, which needs a real measured token count
    (not an estimate) to compare against [models].context_window."""
    return _last_stats


def _log_call(record: dict) -> None:
    BRAIN_LOG_PATH.parent.mkdir(exist_ok=True)
    with BRAIN_LOG_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


async def stream(
    messages: list[dict],
    tools: list[dict] | None = None,
    think: bool = False,
) -> AsyncIterator[str]:
    """Stream assistant tokens for one chat turn. Yields text as it arrives."""
    models = _load_config()
    model = models["primary"]
    endpoint = models["endpoint"]
    keep_alive = models["keep_alive"]

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "think": think,
        "keep_alive": keep_alive,
    }
    if models.get("context_window"):
        payload["options"] = {"num_ctx": models["context_window"]}
    if tools:
        payload["tools"] = tools

    start = time.perf_counter()
    first_token_time = None
    final = None

    client = _get_client(endpoint)
    async with client.stream("POST", "/api/chat", json=payload) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            message = chunk.get("message", {})
            content = message.get("content")
            tool_calls = message.get("tool_calls")

            if first_token_time is None and (content or tool_calls or chunk.get("thinking")):
                first_token_time = time.perf_counter()

            if content:
                yield content
            if tool_calls:
                yield json.dumps({"tool_calls": tool_calls})

            if chunk.get("done"):
                final = chunk

    end = time.perf_counter()
    ttft_ms = round(((first_token_time or end) - start) * 1000, 1)
    duration_s = round(end - start, 3)
    eval_count = final.get("eval_count", 0) if final else 0
    eval_duration_s = (final.get("eval_duration", 0) / 1e9) if final else 0
    tokens_per_sec = round(eval_count / eval_duration_s, 2) if eval_duration_s > 0 else 0.0

    # Ollama's own server-side breakdown (A5: client-measured ttft_ms was ~2x
    # bench.py's number in live use and no client-side reproduction - persona
    # system prompt, accumulated history, concurrent GPU load from XTTS/Whisper -
    # closed the gap. These are the authoritative numbers instead of guessing:
    # load_duration > 0 means Ollama (re)loaded the model this call (cold start,
    # e.g. a keep_alive eviction), prompt_eval_duration is real context-processing
    # time (scales with prompt length, distinct from model load), eval_duration
    # is generation time. ttft_ms above is measured client-side and includes
    # network/httpx overhead on top of these; comparing the two isolates that.
    load_duration_ms = round(final.get("load_duration", 0) / 1e6, 1) if final else None
    prompt_eval_count = final.get("prompt_eval_count") if final else None
    prompt_eval_duration_ms = round(final.get("prompt_eval_duration", 0) / 1e6, 1) if final else None

    stats = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "think": think,
        "ttft_ms": ttft_ms,
        "duration_s": duration_s,
        "tokens": eval_count,
        "tokens_per_sec": tokens_per_sec,
        "load_duration_ms": load_duration_ms,
        "prompt_eval_count": prompt_eval_count,
        "prompt_eval_duration_ms": prompt_eval_duration_ms,
    }
    global _last_stats
    _last_stats = stats
    _log_call(stats)


async def _main() -> None:
    import sys

    prompt = " ".join(sys.argv[1:]) or "What is 2+2? Answer in one word."
    print(f"> {prompt}\n")
    async for token in stream([{"role": "user", "content": prompt}]):
        print(token, end="", flush=True)
    print(f"\n\n(logged to {BRAIN_LOG_PATH})")
    await aclose()


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
