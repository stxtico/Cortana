# A real, double-clickable Cortana.exe (ad hoc, not a PROMPTS.md phase)

The prior launcher work (see `launcher-greeting-selfknowledge.md`) made `ui/src/main.ts` a
real supervisor, but "run it" still meant `electron.exe .` from a shell inside `ui/` - a
command, not something to double-click. This closes that gap: `electron-builder`'s portable
Windows target now produces a real `Cortana.exe`.

## The deliberate problem: ROOT in a packaged layout

`main.ts`'s `ROOT` constant (everything - config, logs, the memory store, spawning the real
Python side - hangs off it) was `path.join(__dirname, "..", "..")`. That only works
unpackaged: `__dirname` is `ui/dist`, so the walk lands on the real checkout. A portable
electron-builder build self-extracts to a fresh `%TEMP%` directory on *every launch* (verified
live below - a real, different temp path each run), so `__dirname` in a packaged build points
into that throwaway copy, not `C:\dev\cortana` - if left alone, config reads, log writes, and
the `uv run python -m services.brain.loop` spawn would all have silently pointed at the wrong
place.

Three options, decided explicitly: an env var (one more manual setup step on top of the
Startup-folder registration already required, easy to forget), a config file beside the exe
(unreliable for the same reason `__dirname` is wrong - the portable build's own
`PORTABLE_EXECUTABLE_DIR` exists specifically because "beside the exe" and "the directory the
unpacked code actually runs from" are different directories for this target), or a hardcoded
path. Picked hardcoded (`const PACKAGED_ROOT = "C:\\dev\\cortana"`, branched on
`app.isPackaged` so dev mode via `npm start` is unchanged) - the user's own framing of this as
an explicitly single-machine tool made this the right call: it's the one option with no extra
runtime indirection to get wrong, and it's a single constant to update if the checkout ever
moves.

## What's actually inside the exe

Worth stating plainly: the packaged exe is a thin shell, not a redistributable copy of
Cortana. `win.loadFile(path.join(ROOT, "ui", "index.html"))` and `index.html`'s own
`<script src="dist/renderer.js">` were already resolving everything - the control panel HTML/
CSS/renderer JS, the character window, the Tray icon lookup - through `ROOT`-relative paths,
not `__dirname`-relative ones. That means only the Electron *main-process* code
(`main.js`/`preload.js`/`character_preload.js`/`character_main.js`/`log_tail.js`/
`py_bridge.js`, the `tsconfig.main.json` set) needs to travel inside the package
(`build.files: ["dist/**/*", "package.json"]`) - the renderer, the Python side, and every
asset still load live from the real `C:\dev\cortana` checkout, exactly as before. Packaging
doesn't freeze or copy the app; it only adds a double-clickable entry point in front of the
same live checkout.

## Two real bugs hit building this, both fixed

1. **electron-builder 26.15.3/26.15.7's own `blockmap.js` does `require()` on
   `@noble/hashes/blake2.js`, which ships pure ESM (`"type": "module"`) as of `@noble/hashes`
   2.x** - `Error [ERR_REQUIRE_ESM]`, a real upstream incompatibility in electron-builder
   itself (its own `app-builder-lib` declares `^2.2.0` against a package whose own dependency
   author moved to ESM-only), not a local misconfiguration. Fixed via an npm `overrides` pin
   (`"@noble/hashes": "1.8.0"`, the last dual CJS/ESM release) - forces every transitive
   resolution to the CJS-compatible version regardless of what `app-builder-lib` itself
   declares.
2. **electron-builder's icon converter rejected the existing multi-size `tray-icon.ico` with
   `Icon must be at least 256x256 pixels, provided: 16x16`**, even though a 256px frame was
   genuinely embedded in it - it reads the *first* frame in the `.ico` directory as canonical,
   and PIL's ICO writer orders frames smallest-first (correct for how Windows picks a tray
   icon size, wrong as a build source). Fixed the standard electron-builder way: added
   `ui/assets/app-icon.png` (a single 1024x1024 PNG, same cyan circled-dot glyph/color as the
   tray icon) as `build.win.icon`, letting electron-builder generate its own properly-ordered
   icon set. `tray-icon.ico` (regenerated with sizes up to 256 along the way, so it also looks
   right if ever reused elsewhere) stays the runtime `Tray` icon, unchanged in behavior.

## Live-tested by an actual double-click, not a shell command

Ran via PowerShell's `Start-Process -FilePath Cortana.exe` - Windows' `ShellExecute` path, the
same mechanism a real double-click invokes, deliberately not `electron.exe .` or any other
dev-shell invocation. Confirmed, each against real external state, not the launcher's own
claim:

- **The temp-extraction problem is real, not theoretical**: the running `Cortana.exe`
  instances resolved to `C:\Users\...\AppData\Local\Temp\<random>\Cortana.exe` - a fresh,
  different path than the one launched (`ui\release\Cortana.exe`), confirming a portable
  build genuinely does self-extract to a throwaway directory on every run.
- **ROOT still resolved correctly despite that**: the spawned `services.brain.loop` and
  `services.daemon.daemon` processes' own `ExecutablePath`/`CommandLine`
  (`C:\dev\cortana\.venv\Scripts\python.exe -m services.brain.loop`, etc. - checked via
  `Get-CimInstance Win32_Process`, not the launcher's own log) point at the real checkout, and
  `logs/launcher.jsonl` shows a genuine `ollama_check: ok=true, pulled=true` against the real
  config - none of that works if `ROOT` had resolved into the temp extraction instead.
- **The window actually rendered**, not just spawned - a real screenshot (not a
  process-list check) shows the control panel with live conversation content across its tabs
  and the character window overlay, both genuinely on screen.
- **The taskbar icon shows the intended glyph** - cropped from that same screenshot, the cyan
  circled-dot design (not Electron's generic default icon), confirming the icon-converter fix
  actually took effect in the built artifact, not just that the build step didn't error.
- **The recursive kill covers the real packaged process tree, not just the top PID**: a single
  `taskkill /pid <main-window-pid> /T /F` (the same call `killProcessTree()` makes) cascaded
  through and terminated all 13 processes it had actually spawned - five `Cortana.exe`
  Electron helpers, the `uv` wrapper layer, and both python trees (loop and daemon) - matching
  exactly what the Tray menu's "Quit Cortana" already does internally, now checked against
  this build's real, different process shape.

Not literally simulated with a mouse - no desktop mouse/keyboard automation is available in
this environment (only browser automation exists here) - `ShellExecute` via `Start-Process` is
the closest available proxy and, unlike `electron.exe .`, exercises the actual portable
self-extraction path a real double-click would trigger. A literal physical click is a 2-second
check the user can still do themselves if they want the very last mile confirmed.

## Not done here

- No code-signing - `signtool.exe` ran during the build (electron-builder's own step) but
  with no real certificate configured, so this is an unsigned exe; Windows SmartScreen may
  warn on first run. Real cert acquisition is a separate, standing decision, not something to
  default into here.
- No auto-update wiring (`electron-updater`, publish config) - the portable target doesn't
  support it, and rebuilding via `npm run dist:exe` after each change is the accepted
  workflow for a single-machine tool, matching the same "not a redistributable installer"
  reasoning as the original launcher decision.
