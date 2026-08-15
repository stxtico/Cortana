"""fetch_url (PROMPTS.md A8, Crawl4AI added A27) - fetches a page and
returns readable text, stripped of markup, navigation, and ads.

Backend is config-selectable ([tools.fetch_url].backend), same
"config-driven backend, one interface underneath" pattern as
[voice].engine in services/voice/tts.py and [tools.web_search].backend -
"trafilatura" (httpx GET + parse, the original A8 method), "crawl4ai"
(tools/_crawl4ai.py, a real headless browser - see that module's
docstring for why it's several times slower and when that's worth
paying), or "auto" (the default): trafilatura first, Crawl4AI only as a
fallback when trafilatura's result comes back empty or under
auto_fallback_min_chars.

"auto" was decided by measurement, not assumption (PROMPTS.md A27
explicitly asked for this rather than picking a default on instinct).
Real numbers, same session, three real pages:
- quotes.toscrape.com (static): trafilatura 0.97s/1654 chars vs
  Crawl4AI 4.09s/4322 chars (first-call cold start) - both extract the
  real content correctly; trafilatura is both faster AND cleaner
  (Crawl4AI's default markdown includes more boilerplate even with
  fit_markdown's PruningContentFilter applied). A static page "tells you
  nothing" about which is better for JS content (PROMPTS.md's own
  framing) - included only as a no-regression baseline.
- Wikipedia (static, substantial real page): trafilatura 0.23s/26999
  chars vs Crawl4AI 0.97s/71679 chars (warm) - same pattern, trafilatura
  faster and tighter.
- quotes.toscrape.com/js/ (the case that actually matters - content
  rendered client-side): trafilatura returned 29 chars, the loading
  shell ("Quotes to Scrape / Login / Next"), missing the real content
  entirely. Crawl4AI returned 1663 chars of the real, correct quote
  content in 1.02s (warm).

Conclusion: trafilatura is faster and cleaner whenever it actually works,
and it's several seconds slower per JS-heavy page to always pay Crawl4AI's
browser-launch cost speculatively. Fallback-on-thin-result gets the
correctness win exactly where it's needed (a genuinely empty/near-empty
trafilatura result is itself the signal that a page needs real JS
rendering) without paying browser cost on the large majority of pages
that don't need it.
"""

import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import httpx
import trafilatura

from tools import _crawl4ai

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"
LOG_PATH = ROOT / "logs" / "fetch_url.jsonl"


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


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f).get("tools", {}).get("fetch_url", {})


def _log(record: dict) -> None:
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}) + "\n")


async def _trafilatura_fetch(url: str) -> str:
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; CortanaAgent/1.0)"})
        resp.raise_for_status()
    return (trafilatura.extract(resp.text, url=url) or "").strip()


async def execute(url: str) -> str:
    config = _load_config()
    backend = config.get("backend", "auto")
    min_chars = config.get("auto_fallback_min_chars", 200)

    if backend == "trafilatura":
        text = await _trafilatura_fetch(url)
        _log({"url": url, "backend_used": "trafilatura", "chars": len(text)})
        return text or "(no readable content extracted from this page)"

    if backend == "crawl4ai":
        text = await _crawl4ai.fetch(url)
        _log({"url": url, "backend_used": "crawl4ai", "chars": len(text)})
        return text or "(no readable content extracted from this page)"

    if backend != "auto":
        raise ValueError(f"Unknown [tools.fetch_url].backend: {backend!r}")

    # auto: trafilatura first (fast, cheap), Crawl4AI only as a fallback
    # when the result is empty or suspiciously short - see module
    # docstring for the measured numbers behind this order.
    traf_text = await _trafilatura_fetch(url)
    if len(traf_text) >= min_chars:
        _log({"url": url, "backend_used": "trafilatura", "chars": len(traf_text)})
        return traf_text

    try:
        c4_text = await _crawl4ai.fetch(url)
    except Exception as exc:
        _log({"url": url, "backend_used": "trafilatura", "chars": len(traf_text), "fallback_attempted": True, "fallback_error": str(exc)})
        return traf_text or f"(no readable content extracted from this page; Crawl4AI fallback also failed: {exc})"

    if len(c4_text) > len(traf_text):
        _log({"url": url, "backend_used": "crawl4ai", "chars": len(c4_text), "fallback_from_trafilatura_chars": len(traf_text)})
        return c4_text or "(no readable content extracted from this page)"

    _log({"url": url, "backend_used": "trafilatura", "chars": len(traf_text), "fallback_attempted": True, "fallback_chars": len(c4_text)})
    return traf_text or "(no readable content extracted from this page)"
