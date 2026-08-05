"""SearXNG backend for web_search.py - self-hosted, zero external API calls or
keys. Requires a running SearXNG instance with JSON output enabled
([tools.web_search].searxng_endpoint in cortana.toml)."""

import httpx


async def available(endpoint: str, timeout: float = 1.0) -> bool:
    """A live reachability check, not a one-time-at-startup latch - the moment
    a real instance comes up, the next agent turn picks it up automatically,
    no process restart needed. Short timeout on purpose: this runs on every
    services/brain/agent.py call, and "nothing's listening" should fail fast,
    not stall the turn waiting on a host that was never going to answer."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{endpoint.rstrip('/')}/search", params={"q": "healthcheck", "format": "json"})
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def search(query: str, max_results: int, endpoint: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{endpoint.rstrip('/')}/search", params={"q": query, "format": "json"})
        resp.raise_for_status()
    data = resp.json()
    results = data.get("results", [])[:max_results]
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
        for r in results
    ]
