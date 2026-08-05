"""Tavily backend for web_search.py. The key comes from TAVILY_API_KEY (.env,
gitignored - see .env.example), never from cortana.toml - CLAUDE.md: no secrets
in the repo. Only ever used in this module, as an outbound Authorization header;
nothing here returns it, and services/brain/agent.py's tool-call logger only
ever sees this function's return value (title/url/content), never the key or
the request headers that carried it.
"""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

_API_URL = "https://api.tavily.com/search"


def available() -> bool:
    load_dotenv()
    return bool(os.environ.get("TAVILY_API_KEY"))


async def search(query: str, max_results: int) -> list[dict]:
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError(
            "TAVILY_API_KEY is not set - add it to .env (see .env.example) to use "
            "the tavily web_search backend, or switch [tools.web_search].backend "
            "to \"searxng\" in cortana.toml."
        )
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            _API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": query, "max_results": max_results},
        )
        resp.raise_for_status()
    data = resp.json()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
        for r in data.get("results", [])
    ]
