# UI depth pass, A29 (ad hoc, not a PROMPTS.md phase)

The A28 craft pass (typography, spacing, legibility contrast) didn't read as a visible
change - the user's words: "too subtle for me to tell it changed." This pass, still via
`apple-design`, deliberately does not touch typography/spacing further and instead pushes on
what A28 left alone: hierarchy, depth, and the treatment of the panels themselves
(`apple-design`'s §12 - "material weight encodes hierarchy").

## What actually changed

`ui/style.css` gained an elevation system (`--elevate-sm`/`--elevate-lg`/`--edge-highlight`)
and two material-weight variables (`--material-chrome`/`--material-recess`), then applied
them everywhere a surface was previously flat:

- **A real cast shadow, not just an accent glow.** Every raised surface (`#frame`, `.turn`,
  `.card`, `.derived-readout`, `.timer-chip`, `#tab-indicator`) previously only had a
  colored glow (`box-shadow: 0 0 Npx rgba(accent...)`), which reads as a light *source*, not
  a physical layer sitting above another - nothing actually looked lifted off the panel
  behind it. Added a black, downward, blurred shadow plus a 1px highlight on each surface's
  own top edge ("light catching the material," apple-design's literal phrasing) alongside the
  existing glow.
- **Chrome/nav/content material-weight split.** The titlebar (heaviest - a dark gradient,
  its own cast shadow separating it from the tab strip below), the tab strip (recessed - an
  inset shadow reads as a groove, not another flat stripe), and the panel content (lightest -
  a static radial glow, no fill) now read as three distinct layers before any label is read,
  not one uniform translucent slab. The memory tab's session list (nav/picker) and entry list
  (content) get the same chrome/content split.
- **A real hero/secondary split on the metrics.** `.derived-readout` (first audio out,
  corrected for double-counting - the one number the latency budget in `CLAUDE.md` treats as
  the headline) previously had nearly the same border/glow/radius as the `.card` grid beneath
  it (per-stage detail) - both read as equally important. Now the hero gets the larger
  `--elevate-lg` shadow tier, a bigger radial glow, and a bumped type size (26px to 34px); the
  cards keep the smaller `--elevate-sm` tier. The shadow-size difference is itself doing the
  hierarchy work, not a label.
- **The active tab is a raised chip, not a colored line.** `#tab-indicator` (the existing
  A28 sliding indicator - unchanged JS, `renderer.ts` still only ever sets
  `transform`/`width`) went from a 2px bottom underline to a full rounded pill filling the
  tab's height, with its own `--elevate-sm` shadow.
- **Conversation bubbles get real elevation and a spatially-anchored tail.** `.turn` gained
  `--elevate-sm`, a per-role gradient tint, and a corner radius that pinches toward whichever
  edge the bubble is already anchored to (apple-design §7 spatial consistency) - the bubble
  now visually belongs to the side it's pinned to, not just positioned there.
- **Section labels got a static marker**, a small accent tick before the text
  (`.section-label::before`), so a structural divider reads as one before the (now slightly
  brighter) muted text is even parsed.

## What deliberately did not change

- **The log feed (`.event-row`) and the latency numbers' own elements (`.big`/`.value`)** -
  zero new `transition`/`animation` properties added to either, same constraint A28 already
  enforced. The surrounding containers (`.card`, `.derived-readout`) gained static shadows,
  but the numbers themselves update exactly as before - a color/size change on state, never a
  per-tick animation.
- **The character layer** (`character.html`/`character_*.ts`/`[ui.hologram]`) - not opened,
  confirmed by diff (`git status` after this pass shows only `ui/style.css` touched).
- **The holographic direction** - same dark base, same cyan accent, same translucent glass;
  this pass added weight and shadow to that language, it didn't replace it with a different
  one.
- **`renderer.ts`/`index.html`** - no DOM or script changes. Every visual change here is pure
  CSS against the existing markup and existing indicator-positioning JS.

## Verified with an actual before/after screenshot, not a description

Built via `apple-design`'s stated process discipline ("test with real people in real
context... review with fresh eyes") as closely as this environment allows: launched the real
dev app twice - once with `ui/style.css` reverted to the committed A28 version (`git stash`),
once with this pass's version - and screenshotted both, side by side, rather than describing
the diff in prose alone. Two real bugs surfaced and were worked around while doing this, not
swept under the "close enough" rug:

- `Get-Process`'s `MainWindowHandle` returned the **character overlay window**, not the
  control panel, when both belong to the same Electron process - the first screenshot attempt
  captured the holographic character, not the panel being tested. Fixed by enumerating all
  visible top-level windows for the process ID directly (`EnumWindows` + title match) instead
  of trusting the one handle Windows happens to call "main."
- **`Maximize` was the actual reliable answer, not manual window positioning.** Every attempt
  to `SetWindowPos` the panel to a specific size on this machine's negative-coordinate
  secondary monitor (see the `ui-craft-pass.md` entry for the same monitor's earlier
  virtual-screen-offset bug) landed content ~140-190px lower than the coordinates specified,
  consistent with the caller process (PowerShell) not being per-monitor-DPI-aware while the
  target window is - manual coordinate math and DPI virtualization disagreed repeatedly.
  `ShowWindow(..., SW_MAXIMIZE)` sidesteps the whole problem: Windows/Electron compute the
  correct physical bounds internally, no caller-side DPI guessing involved.

Side-by-side result: the before-image shows the flat A28 titlebar/tabs/bubbles (thin 2px
underline, no shadows, flat borders); the after-image shows the same live content with
visible cast shadows under every raised surface, a filled tab chip instead of a hairline, and
a visibly heavier/darker titlebar - a real, legible difference at a glance, not one that
requires reading the CSS to notice.
