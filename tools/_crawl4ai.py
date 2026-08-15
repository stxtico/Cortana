"""Shared Crawl4AI helper for fetch_url.py - not itself an agent-callable
tool, same "helper module, not a tool module" shape as tools/_fs.py.

Launches a real headless Chromium per call (via Playwright, already a
project dependency since A18) - genuinely renders client-side JavaScript,
which trafilatura's plain httpx GET + HTML parse structurally cannot do.
Measured directly against trafilatura on real pages (PROMPTS.md A27):
0.1-1s for trafilatura vs 1-4s for Crawl4AI on the same pages - several
times slower, a real cost, not a rounding error. That's why fetch_url.py
only calls this as a fallback when trafilatura's own result comes back
empty or suspiciously short, not as the default path - see that module's
docstring for the measured numbers behind the decision.

Uses fit_markdown (PruningContentFilter-based boilerplate removal) over
raw_markdown - measured directly to be modestly-to-significantly cleaner
(dropped nav-link noise trafilatura's own extraction never included in the
first place) without losing real content, closer to what fetch_url.py's
spec() already promises callers ("stripped of markup, navigation, and
ads") than the unfiltered raw page-to-markdown conversion.
"""

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig, DefaultMarkdownGenerator, PruningContentFilter

_BROWSER_CONFIG = BrowserConfig(headless=True)
_MARKDOWN_GENERATOR = DefaultMarkdownGenerator(content_filter=PruningContentFilter())
_RUN_CONFIG = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, markdown_generator=_MARKDOWN_GENERATOR)


async def fetch(url: str) -> str:
    """A fresh AsyncWebCrawler (and fresh browser) per call, not a
    persistent one reused across calls - deliberately, unlike CLAUDE.md
    rule 7's usual "one instance per process" guidance for models/API
    clients. A held-open browser is real, standing resource cost (a
    Chromium process) for a tool that's called occasionally, not every
    turn - the measured per-call launch cost (a few hundred ms to ~1s once
    past the very first cold start) is the right tradeoff against holding
    a browser open indefinitely for a tool this infrequently used.
    Raises on failure - fetch_url.py's caller decides how to report
    that, same as trafilatura's own httpx call raising on an HTTP error."""
    async with AsyncWebCrawler(config=_BROWSER_CONFIG) as crawler:
        result = await crawler.arun(url=url, config=_RUN_CONFIG)
    if not result.success:
        raise RuntimeError(result.error_message or "Crawl4AI reported failure with no error message")
    markdown = result.markdown
    if markdown is None:
        return ""
    return (markdown.fit_markdown or markdown.raw_markdown or "").strip()
