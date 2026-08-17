# Launcher, session-start greeting, and self-knowledge (ad hoc, not a PROMPTS.md phase)

Three related pieces built together: a real startup launcher, a context-aware greeting
delivered through the existing daemon trigger machinery, and accurate self-knowledge in
`persona.md`.

## 1. Launcher

**Decision: no electron-builder, no separate launcher process. `ui/src/main.ts` itself
became the supervisor.** electron-builder's packaging model (asar-bundled, installer,
uninstaller) fights this project's actual shape - `config/cortana.toml` is hand-edited
constantly, `logs/`/the memory store are live data next to the code, and the Python side is
a real `uv`-managed venv, none of which bundles into a redistributable installer sensibly.
A separate resident Python supervisor (a second process just to launch a third) was rejected
too - `main.ts` already shells out to `uv run python` for config reads and already owns a
real OS surface (windows, now a Tray icon); adding a second supervisor process would be an
extra moving part for no benefit. The actual "executable you run on startup" is just
Electron itself, pointed at the already-built app.

- **Python invocation:** `spawn("uv", ["run", "python", "-m", "services.brain.loop"], {cwd:
  ROOT})` and the daemon equivalent - bare `"uv"` on PATH, the same resolution
  `loadUiConfig()`'s `execFileSync` already proves works from this exact process every
  session, not a new absolute-path lookup.
- **Ollama / missing model:** `checkOllamaAndModel()` is informational only, never blocks
  spawning - Ollama lazy-loads on first request regardless, and the existing model-status
  poll already shows live reachability once the window is up. One new thing: a one-time
  `/api/tags` check for "is `[models].primary` actually pulled" (distinct from "not
  currently resident," which is normal at cold start) - surfaced as a native OS notification,
  not a blocking dialog.
- **How you stop it:** a native Electron Tray icon (`ui/assets/tray-icon.ico`, generated to
  match the existing holographic cyan aesthetic) with "Quit Cortana" - the only normal way to
  stop everything now. `window-all-closed` no longer calls `app.quit()`; closing the control
  panel or character window just closes windows, same as minimizing. `killProcessTree()` uses
  `taskkill /pid <pid> /T /F` (Windows-native recursive kill) rather than a bare
  `process.kill()`, which only ever reaches the top-level `uv.exe` wrapper, not the real
  `python.exe` descendant underneath it - **verified live, not assumed**: killed a real logged
  PID and confirmed via an independent `tasklist` check that `taskkill /T` cascaded through
  `uv.exe` -> an intermediate python.exe -> the actual 2.6GB model-loaded process, all three
  terminated.
- **Startup registration - Startup folder, not Task Scheduler.** This needs a real
  interactive user session (mic, GPU, visible windows) - Startup folder shortcuts run in
  exactly that context by default. Task Scheduler can too ("run only when logged on"), but
  adds real complexity (XML config, a GUI wizard) for zero benefit over a `.lnk` file that's
  visible, simple, and trivially removable. Given to the user to run themselves, not run here:
  ```powershell
  $ws = New-Object -ComObject WScript.Shell
  $lnk = $ws.CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Cortana.lnk")
  $lnk.TargetPath = "C:\dev\cortana\ui\node_modules\electron\dist\electron.exe"
  $lnk.Arguments = "."
  $lnk.WorkingDirectory = "C:\dev\cortana\ui"
  $lnk.Save()
  ```

Live-tested: real spawn (`logs/launcher.jsonl` shows `launcher_start` -> `spawned loop` ->
`spawned daemon` -> `ollama_check: ok=true, pulled=true`), confirmed via `tasklist` that both
Python trees were real, running processes - not just the launcher's own claim.

## 2. Session-start greeting

**Architecture, decided with the user after research surfaced a real constraint the
instruction alone didn't anticipate:** the daemon's `announce()` is print + toast only -
`set_output_handler()` exists but is called nowhere in the repo, so the daemon has zero
speech path. Decided: daemon composes and hands off via a file signal (same idiom as
`playback_state.json`/`listening_state.json`), `loop.py` (the only process that owns TTS)
actually speaks it.

- **New signal files**, both the same tiny atomic-write idiom `playback_state.py` already
  established: `services/memory/session_state.py` (loop.py's `MemoryManager.__post_init__`
  writes `logs/session_state.json` - the same place `session_id` is already generated) and
  `services/daemon/greeting_signal.py` (`daemon_store/pending_greeting.json`, session-id
  checked on read so a stale leftover never gets spoken into an unrelated later session).
- **`services/daemon/session_trigger.py`** - same `is_available()`/`poll()` shape as every
  other trigger. Composes via `brain_client.stream()` (same Ollama server, no new model),
  genuinely conditioned on real, varying context (time-of-day bucket, up to 3 most-recently-
  finished worker tasks via the existing `services/workers/status.py` query, pending timers
  via a new `timers.pending_timers()` reader) rather than a fixed template - **verified live
  that context actually changes the output**, not just that it's non-empty: with no context,
  composed "Hey there, hope your evening is going well..."; with a real pending timer
  present, composed "...your print bed timer is still waiting for you" - genuinely different
  text, correctly referencing the real fact.
- **Explicit bypass, not silent:** `daemon.py`'s `_handle_candidate()` has a dedicated branch
  for `source == "session"` that skips quiet hours, the rate limiter, and the relevance filter
  entirely, logged as such - a greeting only ever fires because the user just launched the app
  themselves, which is the opposite of what those three gates exist to catch.
- **Real timing problem found while wiring this up, not anticipated in the instruction:** the
  daemon's general poll cadence is 30s (tuned for things like "check email," which don't need
  faster checking); `loop.py` can't reasonably wait 30+ seconds at startup for a greeting.
  Fixed by giving `session_trigger` its own faster, decoupled poll loop
  (`[daemon.greeting].poll_interval_s = 2`, run via `asyncio.gather()` alongside the main
  30s loop) - cheap even at 2s, since the real cost (an LLM call) only fires on an actual new
  session_id, not every tick.
- **Double-announcement risk, addressed:** `worker_trigger.py` already announces finished
  tasks on its own. When the greeting mentions specific finished tasks, it seeds their
  `worker_trigger`-shaped ids into the shared `announced_ids` dedup set, so that source
  doesn't separately re-announce the same tasks a moment later.
- **`loop.py`** polls for the handoff file for up to `[brain.greeting].wait_s` (10s default,
  well inside the 2s daemon-side cadence), speaks it via `voice_tts.speak()` before entering
  the wake-word loop, and gives up silently (never hangs) if the daemon isn't running - it's
  optional, per its own module docstring.

Live-tested end to end (not just each piece read back): real session marker written, real
`session_trigger.poll()` run twice with different real context, confirmed the composed text
genuinely differed and correctly referenced the real timer; real handoff round-trip
(including the mismatched-session-id rejection); `loop.py`'s real
`_speak_greeting_if_ready()` exercised both for the found-in-time case (confirmed
`voice_tts.speak()` was actually called with the right text) and the timeout case (confirmed
it returns within the configured bound, doesn't hang). 9/9 checks passed.

## 3. Self-knowledge in `persona.md`

Added a "What she actually is" section: a real local assistant on this machine, built by the
user, named after and modeled on Halo's Cortana without claiming to *be* her - stated
explicitly as load-bearing, not a formality, since identifying as the character pulls toward
roleplay (talking about the Halo array, Master Chief) and away from being useful on whatever's
actually in front of her. Accurate facts added: runs almost entirely locally, with `web_search`/
`fetch_url` (and `computer` driving a real browser, when active) as the real exceptions;
memory persists across sessions in a real store on disk; a real, growing tool set she
shouldn't guess a count for - `capability_list` is the live source of truth, called rather
than recited from a number that would go stale.

Found and fixed three now-stale passages while in there (not asked for, but leaving known-
false statements next to new accurate ones would be an obvious inconsistency): "In Phase 1
she has no CAD tools (that's A14)" and two related "once she can check (A14)" passages - A14
shipped, she has real CAD verification tools now (`cad_generate`/`export_step`/`export_stl`/
`freecad`). Rewrote the surrounding "grounds" reasoning to reflect that a real tool-checked
claim is now possible where it wasn't when that section was written, while keeping the
underlying teaching point (never state an invented number as fact) unchanged.

**A real architectural gap surfaced by this work, not fixed here, per explicit instruction:**
`persona.md` and the tool-calling path never coexist in the same model context today. The
live voice loop (`loop.py:94`, `brain_client.stream(messages, think=False)`) passes no
`tools=` argument at all - `run_agent()` (the only code path that attaches tool specs) is
never called from `loop.py`. Conversely, `run_agent()`'s own callers (the CLI in
`agent.py`, `worker_main.py`) never load `persona.md`. This means the new "call
`capability_list` if asked what you can do" guidance is accurate and forward-correct, but is
currently reachable from a *real spoken conversation* not at all - the model asked "what can
you do" in a live session today can only answer from what `persona.md` itself says, since no
tool schema of any kind reaches it. This mirrors A10's `ask_user` precedent (built as a real
callable tool while "the answer side is keyboard-only until voice reaches agent.py" was an
accepted, stated gap at build time) - the right text is written now, and wiring `agent.py`'s
tool-calling into the live voice loop is real, separate architectural work for its own future
step, not something to bolt on here.

Verified live: `loop.py`'s real `_load_persona()` was called and confirmed to include the new
section, the Halo/`capability_list` references, and confirmed the stale `"A14"` string no
longer appears anywhere in the file.

## Not done here

- The persona/tools context gap above - flagged, not fixed.
- No live end-to-end test through the full mic-to-speaker pipeline (wake word -> greeting
  spoken through real hardware) - verified the logic and the real TTS call site directly
  instead, since driving the actual microphone/wake-word detection isn't something this
  session can trigger from outside.
