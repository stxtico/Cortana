"""Thin async Ollama embeddings client - same shape as services/brain/client.py
(one persistent httpx.AsyncClient, rule 7), talking to nomic-embed-text over
Ollama's /api/embed so retrieval stays fully local, no cloud API key (see
[memory] in cortana.toml for why this replaced Letta's default cloud embedding
path)."""

import json
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"
MEMORY_LOG_PATH = ROOT / "logs" / "memory.jsonl"

_client: httpx.AsyncClient | None = None


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return {**config["memory"], "endpoint": config["models"]["endpoint"]}


def _get_client(endpoint: str) -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=endpoint, timeout=60.0)
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _log(record: dict) -> None:
    MEMORY_LOG_PATH.parent.mkdir(exist_ok=True)
    with MEMORY_LOG_PATH.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}) + "\n")


async def embed(text: str) -> list[float]:
    """Embeds one piece of text. Ollama's /api/embed accepts a batch (`input` as a
    list) but every real call site here embeds one query or one passage at a time,
    so this stays single-text for a simpler caller-side contract."""
    config = _load_config()
    start = time.perf_counter()
    client = _get_client(config["endpoint"])
    resp = await client.post(
        "/api/embed",
        json={
            "model": config["embedding_model"],
            "input": text,
            "keep_alive": config["embedding_keep_alive"],
        },
    )
    resp.raise_for_status()
    data = resp.json()
    vector = data["embeddings"][0]
    duration_ms = round((time.perf_counter() - start) * 1000, 1)
    _log({"stage": "embed", "chars": len(text), "dim": len(vector), "duration_ms": duration_ms})
    return vector
