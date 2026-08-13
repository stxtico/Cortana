# A15 follow-ups — holographic shader, data-texture rain, idle wander

Moved verbatim from CLAUDE.md's Done log (2026-08-12 restructure) — see
CLAUDE.md's Done log for the one-line pointer back to this file.

- **Pre-A16 follow-up - holographic shader overlay
  (`ui/src/character_hologram.ts`), a single combined PIXI filter over the
  Live2D sprite so the Halo 2 Anniversary Cortana look (scanlines, strong
  blue rim glow, a data-texture over the surface, slow vertical drift, mild
  chromatic separation) carries over once the placeholder rig is replaced by
  a real one - explicit ask was to build the look now since it's mostly
  effects, not art. One `PIXI.Filter` (not several chained passes - this
  window runs continuously, so one render-to-texture pass over several was
  the deliberate choice) does chromatic split, ring-sampled rim glow,
  scanlines, a flickering data-texture, and an overall tint in a single
  fragment shader; every knob (`scanline_density`/`scanline_opacity`,
  `rim_color`/`rim_intensity`/`rim_width`, `chromatic_offset`, `drift_speed`,
  `tint_color`/`tint_strength`, `data_texture_opacity`/`data_texture_scale`,
  `enabled`) lives in `[ui.hologram]` in `cortana.toml`, read once at startup
  the same shell-out-to-real-tomllib pattern `[ui]` already uses, not a
  second parser. `tint_color`/`rim_color` default to the same `#39e6ff` as
  `[ui].accent` specifically so the character window and A12's control panel
  read as one system, per the explicit ask.
  **Two real, measured problems, not just a clean first pass.** (1) A hard
  shader link failure on first real run (`PixiJS Error: Could not initialize
  shader`) - found via a temporary `console-message` forward from the
  renderer to main's stdout (removed before finishing, same "temporary
  diagnostic code doesn't ship" precedent as A12/A15), root-caused to a
  precision mismatch: PIXI's own vertex shader declares the auto-provided
  `inputSize`/`inputClamp` uniforms implicitly `highp`, and this fragment
  shader had declared no default float precision at all - WebGL treats a
  precision mismatch on a uniform shared between stages as a link error, not
  a warning. Fixed with an explicit `precision highp float;` plus `highp` on
  those two uniform declarations, matching the vertex stage. Before this fix
  the character window rendered nothing at all (fully blank/transparent,
  indistinguishable at a glance from a working transparent window with
  nothing wrong - only the console forward caught it; a plain screenshot
  comparison would have read as "she's just not there" with no obvious
  cause). (2) Chased what looked like a second real bug - scanline banding
  visible past her silhouette in a zoomed screenshot, consistent with a
  known real risk (Live2D texture atlases commonly leave non-zero RGB baked
  into nominally-transparent margins, which a naive shader can resurrect
  since WebGL's premultiplied blend adds `src.rgb` regardless of `src.alpha`
  being 0). Added an explicit `cX.rgb *= cX.a` premultiply-safety pass on
  every texture sample as the fix - but then measured it rigorously (a
  pixel-diff against a real app-closed baseline screenshot, not another
  eyeball check) and found the fix changed nothing measurable: strictly
  above her hairline, both the pre-fix and post-fix screenshots were
  byte-identical (0/12450 sampled pixels differed) against the app-closed
  baseline - the perceived banding was her own silhouette's rim glow read
  at the wrong crop boundary, not a leak. Kept the premultiply pass anyway
  as correct defensive practice (real, if currently inert, protection
  against exactly this class of bug) but didn't overclaim a fix that the
  data didn't support - full window diff (pre vs. post) showed the same
  ~10800/38400 changed pixels either way, matching her actual rendered
  silhouette plus the intended glow bleed, nothing more.
  **Transparency verified with real numbers, not a visual impression**: a
  pixel-level diff between a real screenshot (character window forced
  topmost via `SetForegroundWindow`/`SetWindowPos` over the real desktop)
  and a real app-closed baseline at the same screen coordinates - 0 delta
  across every sampled pixel above her hairline, confirming the filter never
  writes a visible pixel outside her silhouette plus its own deliberate rim
  bleed; no opaque-rectangle regression anywhere in the window.
  **Frame cost measured, not assumed** (explicit ask, since this window is
  always-on-top and always-running): `character_renderer.ts` samples
  `app.ticker.deltaMS` every tick and reports a rolling avg/max every ~10s
  over IPC; `character_main.ts` appends each report as one structured JSONL
  record to `logs/character_render.jsonl`, same per-service logging
  convention as everywhere else (CLAUDE.md rule 3) - durable, not a one-off
  console print, since the window's cost is worth tracking on an ongoing
  basis. Real measured numbers, steady state, filter active: **4.17ms
  average frame time, max 4.3-4.9ms**, sustained across multiple 10s
  windows. That figure is this machine's real display refresh interval
  (confirmed via `Win32_VideoController` - a 240Hz path was in use), so it
  reads as "the compositor's own vsync tick, hit cleanly every single
  frame" rather than "the shader's raw GPU cost" - the meaningful signal is
  that max never jumped toward a missed-frame multiple (~8.3ms), meaning
  the single combined filter pass never cost enough to drop a frame at this
  resolution (320x480), not that the shader itself takes 4ms.
  Done-when met: `[ui.hologram]`'s parameters are directly editable in
  `cortana.toml` and a restart visibly moves the placeholder rig from
  "anime placeholder" toward a recognizably holographic read (scanlines and
  a strong cyan rim glow read clearly in real screenshots; tint and
  data-texture are present but subtler at default values, which is exactly
  what `tint_strength`/`data_texture_opacity` are there to let the user
  dial up by editing config, per the explicit "I'll tune it by editing
  config" instruction - not chased further here).

- **Data-texture follow-up, same session area: fixed a real "floating in
  front of her" read, added `data_texture_mode` for a real A/B.** The
  original data-texture translated as a unit (drift_speed applied to its own
  y-coordinate) and added light on top of her sampled color - both are
  exactly what reads as a separate layer sliding past in front of a
  character rather than texture on her surface. Three changes, all in the
  new `"surface"` branch of `character_hologram.ts`'s `uDataMode` switch
  (kept the old behavior selectable as `"scroll"` for comparison, per the
  explicit ask): (1) drift_speed now drives scanlines only; the data-texture
  uses `vTextureCoord.y` with no time offset for position, so it's anchored,
  and flickers via a *stepped* time term (`floor(uTime * 2.5)`) instead - a
  refresh/strobe, not a slide. (2) Cells group into irregular horizontal
  bands instead of a uniform grid: a coarse, low-frequency hash warps the
  row coordinate before quantizing (so band edges land at uneven heights,
  not a fixed pitch), each band gets its own density via a per-row hash
  (some bands read dense, some almost empty), and which bands are which
  drifts slowly over time too (a separate, much slower time term than the
  per-cell flicker). (3) Switched from additive (`rgb +=`) to multiplicative
  (`rgb *= 1.0 + variation * ...`, variation centered on zero) - lit cells
  now read as slightly brighter or darker versions of her own sampled
  color, not new light stacked on top of it.
  **Verification methodology note, worth keeping**: the first attempt to
  re-confirm transparency after this change used the same real-desktop
  screenshot pixel-diff method as the original build, and it returned a
  false positive - a real, structured diff (visible as recognizable text
  and button shapes in a rendered diff map, not noise) that looked exactly
  like a leak. Root cause was the method, not the shader: the "baseline"
  screenshot and the "with-app" screenshot were taken far enough apart
  (an app restart, or just several seconds) that this session's own live,
  scrolling desktop content behind the window had genuinely changed in
  between - the diff was real, just not caused by anything this filter
  wrote. Switched to `webContents.capturePage()` (temporary debug code,
  removed before finishing, same as the earlier console-forward) to read
  the renderer's own RGBA buffer directly - immune to background content
  changing between two separate OS screenshots, since there's only one
  capture and it's the actual compositor output. That confirmed cleanly: 0
  of 12,150 sampled pixels above her hairline carried any alpha at all.
  Real lesson for next time this needs checking: prefer `capturePage()`
  over a two-screenshot pixel diff whenever the surrounding desktop can't
  be guaranteed static for the seconds between captures - which, in
  practice on a real working desktop, is most of the time.

- **Data-texture, second follow-up: rewritten as real glyph rain, not
  banded noise.** The banded-flicker version above still read as blocks;
  explicit ask was falling numbers/letters (digital-rain style), built
  properly rather than faked with more noise math.
  **Real glyph atlas, not procedural noise**: `getGlyphAtlas()` renders 32
  characters (digits, a hand-picked subset of uppercase letters avoiding
  lookalikes like O/0 and I/1, and a few geometric marks) into an offscreen
  `<canvas>` once at startup - bold monospace, 48px per cell on an 8x4 grid
  - and hands PIXI a `Texture.from(canvas)` as a second `uGlyphAtlas`
  sampler alongside the model's own texture. This is what makes the shapes
  actual characters instead of a shader-noise approximation of them.
  **Columns, discrete stepping, real head+trail**: her width divides into
  columns (`data_texture_column_width`, px); each column gets its own
  speed (0.6x-1.4x `data_texture_fall_speed`) and phase from a per-column
  hash so they don't fall in lockstep. Each column tracks one descending
  head row via `floor(mod(time*speed + phase*cycleLen, cycleLen) -
  trailLength)` - `floor()` makes the head hold position for a whole row's
  duration then jump exactly one row (the "steps down, doesn't slide"
  requirement), and `mod()` against a cycle length (visible rows + trail +
  a gap) makes each column an endlessly-repeating single stream with a
  real gap between passes, not a static back-to-back field. Brightness is
  `1.0 - dist/trailLength` where `dist` is rows-behind-the-head - 1.0 at
  the lead glyph, fading to 0 at the trail's end, verified by eye via a
  zoomed real screenshot showing a bright head glyph with a visibly dimmer
  one trailing above it, correct direction (bright at the leading/bottom
  edge, fading upward into the trail, matching the classic reference).
  **Glyph identity cycles independently of the fall**: a separate stepped
  time term (`data_texture_glyph_swap_rate`) reseeds each cell's glyph hash
  on its own cadence, so a cell can flicker to a different character while
  sitting in the same row/trail position - this, not the falling motion
  itself, is what was expected to sell "characters" over "pattern," per
  the explicit framing.
  **Multiply/additive both still real options**: `data_texture_mode`
  (`"multiply"` default, `"additive"` legacy-slot) now picks the blend for
  the *same* rain columns rather than switching between two different
  effects - multiply brightens on a glyph stroke and mildly darkens the
  gap around it (modulating her own surface, same reasoning as the
  banded-flicker version it replaced); additive glows `rim_color`
  unconditionally on top, gated to her actual surface either way so
  neither mode ever shows in the rim halo.
  **Legibility checked and corrected, not assumed**: shipped first at
  `column_width=14`, screenshotted and zoomed - individual glyphs ("6",
  "1", "H") were identifiable up close but borderline at the window's real
  on-screen size, exactly the risk flagged going in. Compared directly
  against `column_width=20` at the same real size: a zoomed real
  screenshot showed a clearly legible "H" (bright head) with a "1" trailing
  dimmer below it - both immediately readable as characters, not texture.
  Shipped `20` as the default, not `14`.
  **Transparency and frame cost re-verified after the rewrite, not assumed
  carried over from the last check**: `capturePage()`'s real alpha channel
  showed 0/12150 sampled pixels with any alpha above her hairline (same
  method as the prior false-positive lesson, applied correctly this time),
  and overall lit-pixel coverage (28.7%) matched the pre-rewrite baseline
  almost exactly, before deleting the temporary capture code again.
  `logs/character_render.jsonl` showed the same steady-state 4.17ms
  avg/4.4-4.7ms max frame time as before the rewrite - the second texture
  sampler and per-pixel column math didn't push the single combined filter
  pass past this display's vsync budget.

- **Character window, third follow-up: opacity matched to the control
  panel, idle wandering wired up for real, glyph rain legibility
  re-confirmed.** Three explicit asks in one session.
  **`character_opacity`** (`[ui.hologram]`, default 0.82): matched to
  `[ui].panel_opacity`, the real number, not a guess - `window_opacity` is
  1.0 and (per A12's own comment) doesn't visibly fade anything day to
  day, so 0.82 is the value that actually determines what the panel reads
  as. Applied as `model.alpha` in `character_renderer.ts`, after the
  hologram filter runs - a uniform fade over the whole rendered sprite
  (glow/scanlines/glyph rain included), the same role `panel_opacity`
  plays for the control panel's glass. Confirmed visually via a real
  screenshot - a subtle, even translucency, not a dramatic change at 0.82.
  **Idle wandering, built as a real autonomous trigger, not another
  hotkey-only mechanism.** `[ui.wander]` (`enabled`, `interval_min_s`/
  `interval_max_s`, `distance_min_px`/`distance_max_px`).
  `wanderNearby()` reuses `animateX()` directly - a single same-monitor
  move, not `walkToDisplay()`'s exit/jump/enter sequence, which stays
  reserved for actual monitor changes. `startIdleWander()` draws a fresh
  random interval each cycle and checks suppression at fire time, not
  schedule time - a cycle landing mid-conversation is skipped outright and
  the next interval drawn fresh, not queued to fire the instant she goes
  quiet.
  **Suppression required a real "is she listening" signal that plainly did
  not exist anywhere in the codebase before this - built it, not faked
  it.** `services/voice/playback_state.py`'s existing `active` field
  already covered speaking (already tailed for lip sync, just added to the
  same read). Listening had no equivalent - `pipeline.py`'s `state`
  variable was purely in-process, never written anywhere external.
  Mirrored `playback_state.py`'s exact pattern into a new
  `services/ears/listening_state.py` (`mark_active`/`mark_idle`/
  `is_active`, same atomic-write-with-retry, same dependency-free-import
  reasoning) and routed every one of `pipeline.py`'s ~9 `state = "..."`
  assignments through one new `_set_state()` helper so there's exactly one
  place that maps pipeline states to the external signal, not nine
  separate call sites that could drift out of sync. "Listening" (in the
  sense this suppression cares about) is anything other than idly waiting
  for the wake word - `recording` and `awaiting_resume` both count.
  Caught a real ordering bug immediately via a plain import test (not
  live, just `python -c "import services.ears.pipeline"`): the first
  version defined `_set_state` after the line that used it to initialize
  `state`, a `NameError` waiting to happen the moment the module actually
  ran. Fixed by moving the helper's definition above its first use;
  re-verified with a real `mark_active()`/`is_active()`/`mark_idle()`
  round trip plus the same import check, clean.
  **Verified end-to-end for real, not just unit-by-piece**: temporarily
  set `[ui.wander]`'s interval to 3-5s (from the real 120-360s) and drove
  four checks via live `GetWindowRect` readings against the real running
  window rather than trusting the code read correctly - (1) she moved
  repeatedly and unprompted with the app just sitting there, deltas
  consistent with the configured distance range accumulated over however
  many 3-5s cycles elapsed between readings; (2) writing
  `playback_state.json`'s `active: true` by hand froze her in place for a
  full 10s spanning 2-3 would-be cycles, confirmed via unchanged
  `GetWindowRect` coordinates; (3) clearing it let wandering resume
  (one check window landed in a between-cycles gap and briefly looked
  stalled - re-checked a few seconds later and she had moved, a timing
  artifact of the test, not a real bug); (4) the same suppression
  independently confirmed via `listening_state.json`'s `active: true`,
  10s frozen. Y-coordinate never changed across any reading, confirming
  wander only moves along X as designed. Config restored to the real
  120-360s/60-220px defaults and all test-written state files cleaned up
  afterward - a final clean launch confirmed she still starts at the
  normal default position (2200,912) with no crash.
  **Glyph rain legibility re-checked fresh, not assumed from last
  session's crops**: a new native-resolution screenshot (not zoomed) with
  the current shipped defaults (`column_width=20`) showed individually
  legible glyphs ("M", "K") on her body at actual on-screen size,
  confirming last session's tuning call still holds.

