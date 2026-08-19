# A30 - free redesign pass (ad hoc, not a PROMPTS.md phase)

Explicit instruction: no constraints beyond keeping the 4 tabs functional, leaving the
character layer alone unless there's a real argument otherwise, and staying readable at a
glance as a live debug panel. Visual direction (layout, hierarchy, color, type, motion,
structure) was left entirely to this pass.

## The case against keeping the holographic-hud skin

Made explicitly, since the instruction invited it: the character window already carries "this
is Cortana" - blue glow, scanlines, the full sci-fi identity, and it stays exactly as it was
(not touched - confirmed via `git status` showing only `ui/index.html`, `ui/style.css`, and
`ui/src/renderer.ts` changed). The control panel's job is different: it's the thing actually
read while debugging a real latency spike or checking what a tool call just did, fast, against
whatever's on the real desktop behind it. Reusing the character's skin for a data-dense
utility is the opposite of apple-design's own "flexibility" principle ("design for different
contexts... adapt to the platform and the situation") - a hologram reads great as ambient
presence and fights legibility as an instrument. This session's entire prior chapter (the
opacity-fix work) is a direct symptom of that fight: a HUD skin needed real engineering effort
just to make its own text readable over a busy desktop.

## What changed

- **Vertical sidebar nav, not a horizontal tab strip.** A persistent rail (`#sidebar`) with
  the 4 sections always visible - "where am I / where can I go" is answerable at a glance
  (apple-design SS16 wayfinding), the convention every real dev tool uses (VS Code's activity
  bar, Linear's sidebar). `renderer.ts`'s `moveTabIndicator()` now positions on the Y axis
  (`offsetTop`/`height`) instead of X (`offsetLeft`/`width`) - the only JS change in this
  pass; the click-handling logic itself is untouched, since it only ever cared about
  `.tab-btn`/`data-tab`, never physical layout.
- **Neutral graphite base, not blue-black.** `--base-rgb` moved from `6, 10, 16` (blue-tinted)
  to `9, 10, 12` (neutral) - the one variable value changed, not a new property (this session
  already hit an unexplained case where brand-new custom properties silently failed to
  resolve in this Electron build; edits to existing, proven-working properties were fine
  throughout). A neutral base means real status colors (green/amber/red) read as themselves
  instead of fighting a cyan cast.
- **Cyan demoted to a precise accent, not smeared everywhere.** A28/A29 put an
  accent-tinted border or glow on nearly every element. Here it marks exactly three things:
  the sidebar glyph, the active-nav indicator bar, and the Latency hero readout's left edge -
  everywhere else uses neutral grays and real status colors. Restraint is itself an
  apple-design principle (SS6, "simplicity - not minimalism... every element earns its
  place").
- **Every content surface stays the opaque, `isolation: isolate`-backed pattern this
  session's opacity fix proved reliable** (see `ui-opacity-fix.md`) - reused wholesale across
  the new sidebar, `.scroll-list`, `.turn`, `.card`, `.derived-readout`, and both memory
  columns, not reinvented.
- **The Latency hero number is still the loudest thing in its tab** (unchanged intent from
  A29, executed with the new palette) - a left-accent-bar strip, 40px monospace, its own
  shadow tier, clearly ahead of the supporting card grid beneath it.

## What didn't change

- `character.html`/`character_*.ts`/`[ui.hologram]` - untouched, confirmed by diff, per the
  explicit note that the character layer should stay unless there's a real argument for
  touching it (there wasn't one here - the argument was specifically that the *panel* and the
  *character* should diverge, not that the character itself was wrong).
- All four tab IDs, every element ID/class `renderer.ts` queries (`conversation-list`,
  `tools-list`, `pending-timers`, `latency-derived`, `latency-cards`, `latency-list`,
  `memory-sessions`, `memory-entries`, `model-badge`, `btn-min`/`btn-max`/`btn-close`) -
  unchanged, so every existing data-wiring path (log tailing, memory edit/delete, latency
  polling, timer chips) keeps working without touching `main.ts` or the IPC layer at all.
- No new motion beyond what A28/A29 already justified - the log feed and the latency numbers
  themselves still get zero animation; only the sidebar indicator's axis changed, not whether
  it moves.

## Verified live, with an actual screenshot comparison

Rebuilt (`npm run build`, clean) and launched the real dev app. Switching tabs for the
screenshot set used Playwright's page-level `click()` over this session's already-open CDP
connection - deliberately not `win32api` mouse simulation, which moves the real OS cursor and
risks clicking something unintended; a page-level click dispatches synthetic input inside the
renderer only. Confirmed live, not just described: the sidebar nav switches correctly with the
indicator sliding to the right item, the Latency tab's real card grid and raw-event feed
render with full legibility and correct status coloring, and the Memory tab's two-column
session/entry layout still queries the real store (`services/memory/store.py` via
`scripts/memory.py`) and renders its actual "No sessions recorded yet" response.

One process note carried over from the opacity-fix work: earlier verification in this same
session had (before being caught and stopped) used a topmost window positioned over the
user's primary, actively-used monitor. All screenshots for this redesign pass used only the
confirmed-empty secondary monitor, per the user's explicit instruction to use the
`SW_MAXIMIZE`/`EnumWindows` approach "rather than rediscovering those bugs."
