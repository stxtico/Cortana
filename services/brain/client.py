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


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    models = config["models"]
    if not models["primary"]:
        raise SystemExit(f"[models].primary is empty in {CONFIG_PATH}")
    return models


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
    if tools:
        payload["tools"] = tools

    start = time.perf_counter()
    first_token_time = None
    final = None

    async with httpx.AsyncClient(base_url=endpoint, timeout=120.0) as client:
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

    _log_call({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "think": think,
        "ttft_ms": ttft_ms,
        "duration_s": duration_s,
        "tokens": eval_count,
        "tokens_per_sec": tokens_per_sec,
    })


async def _main() -> None:
    import sys

    prompt = " ".join(sys.argv[1:]) or "What is 2+2? Answer in one word."
    print(f"> {prompt}\n")
    async for token in stream([{"role": "user", "content": prompt}]):
        print(token, end="", flush=True)
    print(f"\n\n(logged to {BRAIN_LOG_PATH})")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
