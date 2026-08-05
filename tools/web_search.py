"""web_search (PROMPTS.md A8) - config-driven backend ([tools.web_search].backend
in cortana.toml), same dispatch pattern as [voice].engine in
services/voice/tts.py. "tavily" (tools/_search_tavily.py, needs TAVILY_API_KEY)
or "searxng" (tools/_search_searxng.py, self-hosted, zero external calls)
implement the same search(query, max_results) -> list[dict] interface
underneath - switching backends is a config change, not a rewrite.

Only title/url/content ever leave this module - see _search_tavily.py's
docstring for why the API key can't leak into services/brain/agent.py's
tool-call log even by accident.
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

def spec() -> dict:
    """A function, not a static dict - see tools/list_dir.py's spec() for why
    the other tools here need this; kept a function for a uniform shape across
    every tool module even though this one's description is static today."""
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web and return a short list of relevant results (title, url, snippet).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                },
                "required": ["query"],
            },
        },
    }


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("tools", {}).get("web_search", {})


async def is_available() -> bool:
    """services/brain/agent.py checks this before offering web_search to the
    model at all - PROMPTS.md A8 (deferred pending real search infrastructure,
    see CLAUDE.md): no live SearXNG instance and no Tavily key means there's
    nothing here that would actually work, so it shouldn't be in the tool list
    for the model to pick and fail on."""
    config = _load_config()
    backend = config.get("backend", "tavily")
    if backend == "tavily":
        from tools import _search_tavily
        return _search_tavily.available()
    if backend == "searxng":
        from tools import _search_searxng
        return await _search_searxng.available(config.get("searxng_endpoint", "http://localhost:8080"))
    return False


async def execute(query: str) -> str:
    config = _load_config()
    backend = config.get("backend", "tavily")
    max_results = config.get("max_results", 5)

    if backend == "tavily":
        from tools import _search_tavily
        results = await _search_tavily.search(query, max_results)
    elif backend == "searxng":
        from tools import _search_searxng
        results = await _search_searxng.search(query, max_results, config.get("searxng_endpoint", "http://localhost:8080"))
    else:
        raise ValueError(f"Unknown [tools.web_search].backend: {backend!r}")

    if not results:
        return "No results found."
    return "\n".join(f"- {r['title']} ({r['url']})\n  {r['content']}" for r in results)
