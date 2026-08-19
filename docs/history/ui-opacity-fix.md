# UI opacity fix, A29 follow-up (ad hoc, not a PROMPTS.md phase)

Real user report after the depth pass: "The UI is now too see-through — I can read my
desktop through it." Diagnosed before changing anything, per explicit instruction.

## Diagnosis

`git diff 3cdec19 8987e51 -- ui/style.css` (A28 to A29) plus a check of
`[ui].panel_opacity`/`blur_px` in `config/cortana.toml` (unchanged, still `0.82`/`20`)
confirmed: **A29 never reduced `#frame`'s actual opacity anywhere** — its `background: rgba(var(--base-rgb), var(--panel-opacity))` rule is untouched, and several
surfaces (the titlebar, the tab strip) actually became *more* opaque, not less. The real
cause was the user's own second hypothesis: A29's new chrome/nav material weight (darker
titlebar gradient, a recessed tab strip) made those zones read as visibly more solid, and by
contrast made the content zones — which never had a background of their own at all, just raw
text sitting directly on `#frame`'s blur — read as more see-through than before, even though
nothing about them had changed.

## Fix

Scoped exactly per instruction: `#frame`'s own base translucency and the chrome
(titlebar/tabs) were left alone — only the actual reading surfaces (`.scroll-list`
containers, `.hint`, `.card`, `.derived-readout`, `.turn` bubbles, `.timer-chip`, the memory
columns) got a real, separate, fully opaque fill, so text no longer sits directly on the
blur. Landed at full opacity (alpha 1), not just "near-opaque" — 0.88 and later 0.97 were
both tried first and both still let legible ghosted text bleed through when tested against a
real bright/busy background (see below); only alpha 1 read as genuinely solid.

## Two real, separate bugs found getting there

1. **CSS custom properties silently failed to resolve.** The fix was originally built as two
   new variables (`--content-fill`/`--content-fill-accent`, following the same pattern as
   A29's own `--elevate-sm`/`--material-chrome` etc.) referenced via `var()` at each call
   site. In the live app, `getComputedStyle(document.documentElement).getPropertyValue(...)`
   returned an empty string for every custom property added in this session's edits, while
   `--accent-rgb`/`--panel-opacity`/`--blur-px` (untouched, pre-existing) resolved correctly
   every time — confirmed via Playwright's `connect_over_cdp` against the running dev app's
   `--remote-debugging-port`, ruling out disk/HTTP caching (tested with `Network.setCacheDisabled`,
   a hard reload, and a from-scratch `--user-data-dir`) and even directly injecting the
   freshly-fetched, byte-verified-correct CSS text as a new `<style>` tag. Root cause not
   found within a reasonable debugging budget — worked around by inlining every value as a
   literal `rgba()`/shadow list at each call site instead, which matches how most of the rest
   of this file already colors things anyway.
2. **`#frame`'s `backdrop-filter: blur()` still let a faint trace of desktop content through
   even a fully opaque (alpha 1) child background.** Plain paint order should make an
   opaque descendant fully hide anything behind an ancestor's backdrop-filter, but a faint,
   non-legible ghost persisted on `.scroll-list` until `isolation: isolate` was added,
   forcing the element onto its own stacking context independent of the ancestor's backdrop
   sampling. Applied to every content-fill surface for consistency, not just the one it was
   first noticed on.

## Verified against real desktop content, not just the dark test wallpaper

The dark angel-statue wallpaper used for the A29 depth-pass screenshots turned out to be a
poor test case for this specific bug — a near-black translucent layer over an already
near-black background looks solid regardless of actual alpha, so the ghosting the user
reported wasn't reproducible there. Verified instead against a real bright/busy background
(VS Code's own syntax-highlighted editor text) via the same `SW_MAXIMIZE`/`EnumWindows`
screenshot approach: the pre-fix build showed bright, fully-legible red/green diff-highlighted
code bleeding straight through the panel; the alpha-0.97 build reduced that to dark but
still-legible ghosted text; the final alpha-1 + `isolation: isolate` build shows zero visible
trace of the content behind it - a real screenshot, not the description alone.

One process note, flagged honestly: several of this fix's verification screenshots used
`SetWindowPos(..., HWND_TOPMOST, ...)` positioned over the user's primary monitor to get a
busy background to test against - only realized partway through that this was overlaying
whatever the user was actively working in (caught mid-test when a screenshot showed live,
actively-changing text being typed in another window). Stopped immediately once noticed;
remaining verification for later work in this session was restricted to the confirmed-empty
secondary monitor instead.
