"""fetch_url (PROMPTS.md A8) - httpx + trafilatura, strips a page to readable
text. Truncation to a safe context size happens in services/brain/agent.py's
dispatcher (config-driven, shared across every tool) - not duplicated here.
"""

import httpx
import trafilatura

def spec() -> dict:
    """A function, not a static dict - see tools/list_dir.py's spec() for why
    the other tools here need this; fetch_url's description happens to be
    static today, kept a function anyway so every tool module has the same
    shape for services/brain/agent.py to call uniformly."""
    return {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch a web page and return its readable text content, stripped of markup, navigation, and ads.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch."},
                },
                "required": ["url"],
            },
        },
    }


async def execute(url: str) -> str:
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; CortanaAgent/1.0)"})
        resp.raise_for_status()
    text = trafilatura.extract(resp.text, url=url)
    return text or "(no readable content extracted from this page)"
