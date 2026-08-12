"""Playwright browser target resolution (PROMPTS.md A18) - the second
resolution tier, browser targets only (everything else goes through
tools/_computer_uia.py's Windows UI Automation instead - Chrome's own UIA
tree is real but noisy for web content, Playwright's selector engine is
purpose-built for it).

Deliberately dormant by design, not a stub pretending to work: is_available()
only ever checks whether something is ALREADY listening on a Chrome DevTools
Protocol debug port (rule 10 - an availability check must be incapable of
starting or changing anything, same discipline as tools/_outlook.py's
Outlook check). It never launches a browser and never attaches to whatever
browser the user happens to already be using day to day. That's a deliberate,
explicit scope decision, not an oversight: attaching to a live, authenticated
browser profile is the single broadest capability in this whole build - an
authenticated CDP session reaches everything that profile is logged into.
Enabling that for real (launching Chrome with --remote-debugging-port pointed
at the user's actual profile, or attaching to an already-running one) is a
separate decision with its own scope discussion, per explicit instruction -
not something this tier does on its own or that flipping a config value
silently turns on. Until that decision is made, this tier stays real code
that activates automatically the moment a debug-port Chrome happens to exist
(same "the real dependency exists, so it works" pattern as
tools/web_search.py's backend gating), and inert otherwise.
"""

from dataclasses import dataclass

import httpx
from playwright.async_api import async_playwright

CDP_TIMEOUT_S = 1.0  # short - is_available() runs on every offer check (agent.py's _drop_unavailable_tools), same rule-10 cheapness requirement as every other gated tool


@dataclass
class ResolvedElement:
    name: str
    center_x: int
    center_y: int


def cdp_endpoint(port: int) -> str:
    return f"http://localhost:{port}"


async def is_available(port: int) -> bool:
    """Read-only: does anything already answer on the CDP debug port. Never
    launches, never attaches, never sends anything beyond a version probe -
    the same guarantee tools/_outlook.py's is_available() makes for Outlook."""
    try:
        async with httpx.AsyncClient(timeout=CDP_TIMEOUT_S) as client:
            resp = await client.get(f"{cdp_endpoint(port)}/json/version")
            return resp.status_code == 200
    except Exception:
        return False


async def resolve(port: int, selector: str) -> ResolvedElement | None:
    """Attaches to an already-running, already-debug-port-enabled Chrome (see
    module docstring - this never launches one) and resolves selector (a real
    Playwright locator string - role/text/accessible-name based, e.g.
    "role=button[name='Submit']" or "text=Submit") against the first page
    that has a match. Returns the element's on-SCREEN center point, not just
    its in-page position - a page-viewport-relative bounding box alone isn't
    enough to click through tools/_computer_input.py's OS-level cursor
    synthesis, which operates in absolute desktop pixel coordinates.
    window.screenX/screenY (the outer window's real OS position) plus the
    outerHeight-innerHeight chrome offset (tab strip + address bar height)
    convert the two spaces correctly - deliberately not using Playwright's
    private/internal CDP session API for this, since window.screenX and
    friends are ordinary, stable, documented browser properties."""
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_endpoint(port))
        try:
            for context in browser.contexts:
                for page in context.pages:
                    locator = page.locator(selector).first
                    try:
                        box = await locator.bounding_box(timeout=1000)
                    except Exception:
                        continue
                    if box is None:
                        continue
                    frame = await page.evaluate(
                        "() => ({x: window.screenX, y: window.screenY, "
                        "chromeH: window.outerHeight - window.innerHeight, "
                        "chromeW: window.outerWidth - window.innerWidth})"
                    )
                    name = await locator.text_content() or selector
                    center_x = round(frame["x"] + frame["chromeW"] / 2 + box["x"] + box["width"] / 2)
                    center_y = round(frame["y"] + frame["chromeH"] + box["y"] + box["height"] / 2)
                    return ResolvedElement(name=name.strip(), center_x=center_x, center_y=center_y)
            return None
        finally:
            await browser.close()
