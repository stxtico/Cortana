# UI craft pass — control panel (not a PROMPTS.md phase)

Ad hoc, user-requested — not from `PROMPTS.md`'s roadmap, so it isn't numbered as an
`A`-phase the way A24–A27 are. Recorded here anyway per this project's own documentation
habit: real decisions made, a real bug found, worth keeping for the same reason every other
session's work is.

## What this was

Install [emilkowalski/skills](https://skills.sh/emilkowalski/skills) (`emil-design-eng`,
`apple-design`, `animate`, `animation-vocabulary`, plus six more) and use them for a craft
pass on the Electron control panel (`ui/index.html`, `ui/style.css`, `ui/src/renderer.ts`) -
motion, spacing, easing, typography, legibility. Explicitly **not** the character layer
(`character.html`, `character_renderer.ts`, `character_hologram.ts`, `character_main.ts`,
`[ui.hologram]`) - untouched, confirmed by diff.

## Installing the skills needed a real workaround

`npx skills add emilkowalski/skills` failed outright on this machine's default Node
(`v20.11.1` via a system PATH entry) - the `skills` CLI imports `node:util`'s `styleText`
export, added in Node 21.7/22. This machine already had `nvm-windows` with Node 18/20
installed but not 22. Installed Node 22.23.2 via the existing `nvm install 22` rather than
switching the global active version (`nvm use`), which would have changed Node version for
everything else on the machine, not just this one command - invoked `npx` directly from the
v22 install directory instead (`.../nvm/v22.23.2/npx.cmd`, with that directory prepended to
`PATH` for just that one call). 10 skills installed cleanly under `.agents/skills/`
(real files) with `.claude/skills/` symlinks for discovery (that directory is gitignored,
correctly - `.agents/skills/` and `skills-lock.json` are the real, committed content).

## Scope check found a real gap, and the user cut scope in response

Constraint #3 as given ("the panel now surfaces computer_stats, capability_list's
three-state output, worker status, and the memory inspector") was checked against the actual
code before designing around it, per the project's own working-style rule ("if something in
PLAN.md seems wrong given what you've found, say so rather than silently working around it" -
the same principle applied here to a premise inside this session's own instructions, not just
`PLAN.md`). `git log -- ui/` shows nothing has touched `ui/` since A18; `renderer.ts`/
`main.ts`/`preload.ts`/`types.d.ts` have zero mentions of `computer_stats`, `capability_list`,
or workers. The panel is still exactly the four original A12 tabs.

Surfaced this before designing rather than either quietly building three new panels or
quietly ignoring the stated premise. The user's response: scope to the four existing tabs
only, and explicitly **not** as a deferred "build these later" - as an open, unresolved
question. Their own reasoning, worth keeping verbatim-adjacent: a redesign and a feature
build are different jobs (mixing them means you can't tell if a new panel is good or just
new), and it isn't obviously settled that `computer_stats` (an occasional CLI) or
`capability_list` (a conversational tool call) belong in this panel at all - only worker
status probably does, and that deserves its own design thought, not a slot filled during a
visual pass.

**Open question, not a task:** should `computer_stats`, `capability_list`, or worker status
get a surface in this panel at all, and if so, which ones and in what shape? Not decided
here.

## What changed

Same holographic direction throughout - frameless, translucent, cyan accent, dark base, same
four tabs, same information density. Craft, not a restyle:

- **Typography** (`apple-design` §15): `font-variant-numeric: tabular-nums` on every numeric
  readout (latency cards, derived total, timer chip countdowns, event-row timestamps) - a
  legibility fix, not motion: fixed-width digits mean a changing number doesn't visibly
  reflow its neighbors. Negative letter-spacing on large display numbers (`-0.02em` on the
  26px derived total, `-0.01em` on 19px card values) - large text wants tighter tracking as
  it grows; small labels were already correctly positive-tracked and left alone.
- **Spacing**: a real 5-step scale (`--space-1` through `--space-5`, 4px-24px) replacing the
  ad hoc 6px/7px/9px/11px/14px mix, applied without changing overall density - this is a
  debug surface (constraint #1: legibility beats polish where they conflict), not a
  consumer app that benefits from more whitespace.
- **Legibility contrast** (`apple-design`'s "vibrancy" rule - flat gray text over a
  translucent/blurred surface loses contrast fastest): brightened every muted secondary
  color (`.hint`, `.section-label`, timestamps, card labels, target/sub text) by roughly one
  step. Nothing structural changed, only how legible the existing hierarchy actually reads
  against the real blurred background.
- **Motion, scoped deliberately to transitions and state changes** (constraint #2, followed
  literally): a sliding tab indicator (`#tab-indicator`, positioned by real button
  `offsetLeft`/`offsetWidth` in `renderer.ts`, not guessed percentages) replacing each
  button's static underline - occasional, user-triggered, a "spatial consistency" purpose
  (`apple-design` §7), not decoration. A short settle-in on tab-panel activation and the
  memory entry editor opening (`opacity`/`translateY(4px)`, never `scale(0)`). A smooth color
  transition on the model badge across its occasional state changes (idle/active/
  unreachable). Discrete button press feedback (`scale(0.94)` on `:active`) on window
  controls and entry-row edit/delete buttons - a per-click response, not continuous data.
  Memory entry deletion collapses (`opacity`/`transform`/`max-height` together) instead of
  vanishing with `.remove()`, waiting on the real `max-height` `transitionend` (not a fixed
  `setTimeout`, and not the first-arriving `transitionend` of several staggered properties -
  see the renderer.ts comment for why that distinction mattered). Custom easing throughout
  (`--ease-out: cubic-bezier(0.23, 1, 0.32, 1)`, never `ease-in`) per `emil-design-eng`.
  `prefers-reduced-motion` cross-fades instead of removing feedback entirely, per
  `apple-design` §14.
- **Explicitly NOT animated, per constraint #2 stated directly**: new rows appearing in the
  Tools/Activity feed and the Latency raw-stage-events feed (these append many times a
  second during real activity), and the latency numbers themselves (update every turn).
  `tabular-nums` above is typographic, not motion - it stops digits from visibly shifting
  width without any animation being involved at all.

## Verified live, not just read back

Built cleanly (`tsc -p tsconfig.main.json && tsc -p tsconfig.renderer.json`, zero errors).
Launched the real Electron app, drove it through all three non-default tabs
(`tools/computer.py`'s real `click` action against the real UIA tree - "Latency", "Memory" -
not a mocked click) and screenshotted each: the tab indicator correctly followed the active
tab's real position/width, latency cards and raw-event digits read cleanly with tabular
alignment, the memory tab's empty-state hint rendered with the brightened contrast.

**A real bug found taking the "after" screenshot, not a UI bug - a bug in the screenshot
tooling itself.** First attempts cropped a completely different window (Spotify) instead of
the control panel, silently - no error, just the wrong pixels. Root cause: this machine's
dual-monitor layout has a monitor positioned left of the primary, so
`win32gui.GetWindowRect()`'s virtual-screen coordinates are genuinely negative in that
region, while `PIL.ImageGrab.grab(all_screens=True)`'s resulting image's pixel `(0,0)` is the
virtual desktop's top-left bounding corner - itself at that same negative offset. Cropping
with the raw `GetWindowRect()` values directly is off by exactly that origin. Fixed by
reading `GetSystemMetrics(SM_XVIRTUALSCREEN/SM_YVIRTUALSCREEN)` and subtracting before
cropping - confirmed correct once the crop actually showed the control panel instead of
whatever window happened to occupy that same pixel region on a different monitor.
