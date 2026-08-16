# CORTANA

Local voice assistant + agent. Companion and work tool. Read `PLAN.md` for full context
and rationale — this file is the operating summary.

## Current state

**Phase:** 8 — The character (A15 done, holographic shader follow-up done); A18
(computer use), A19 (marketing pipeline), A20 (attribution loop), A21 (delegation), A22
(grounding upgrade + screen awareness, all four steps), A23 (tool wave 1, the system
layer), A24 (tool wave 2, deliverables), A25 (default-allow computer use), A26
(FreeCAD scripting), and A27 (Crawl4AI) have all run ahead of the documented order,
per explicit instruction each time — that closes out TRACK A entirely; everything
remaining in PROMPTS.md is TRACK B (requires the Spark) or A16/A17. A16/A17
(camera/ambient awareness) are the next fully-untouched
phase.
**Hardware:** RTX 3080 Ti (12GB VRAM), i9-12900K, 32GB RAM, dual 1440p, Windows 11
**Model:** Gemma 4 Unified, elastic (Q4, multimodal — covers vision too), Ollama tag
`gemma4:e4b` (switched from `gemma4:12b` — 3.2GB resident vs ~9.8GB, validated against
A8's tool-calling demands first, see Done below)

> Update this block every session. It's the first thing to read and the thing most likely
> to be stale.

**Done:**
- **A0** — repo skeleton, uv project, `config/cortana.toml`, `scripts/bench.py`. Baseline
  TTFT/tok-s measured on `gemma4:12b`. [docs/history/A0.md](docs/history/A0.md)
- **A1** — `services/brain/client.py`: async Ollama streaming client (`stream()`, OpenAI-format
  tool calling passed through), one persistent httpx client per process (rule 7 — a fresh
  client per call had cost ~280ms). [docs/history/A1.md](docs/history/A1.md)
- **A2** — `services/ears/{wake,vad,stt}.py` + `pipeline.py`: silero-vad + faster-whisper
  large-v3-turbo on GPU, plus a real trained `hey_cortana` wake model (beat the
  `hey_jarvis`+STT-verification baseline outright: 0 false accepts vs. 2-of-6).
  [docs/history/A2.md](docs/history/A2.md)
- **A3** — `services/voice/{engine,kokoro_engine,xtts_engine,tts}.py`: engine-agnostic TTS
  interface, switched `[voice].engine` to XTTS with cached reference latents and a pipelined
  `speak_stream()`. [docs/history/A3.md](docs/history/A3.md)
- **Voice strategy tuning** (pre-A4) — VAD pause-threshold investigation (closed, don't
  retune — see the latency budget below), full-stack VRAM budget across every model
  combination tried, six `[voice].strategy` streaming strategies culminating in
  `buffered_stream` (the shipped default — matches `inference_stream`'s zero mid-response
  gaps at roughly half its TTFA).
  [docs/history/voice-strategy-tuning.md](docs/history/voice-strategy-tuning.md)
- **Backchannel verification & quality** (pre-A4) — live end-to-end trail-off/backchannel/
  resume test (two full cycles, correctly fused), four listening-driven backchannel fixes
  (lexical words only, `soft` reference at `speed=0.88`, cross-utterance volume ramp, master
  output gain).
  [docs/history/backchannel-verification.md](docs/history/backchannel-verification.md)
- **A4** — `services/brain/loop.py` closes the loop end-to-end (wake → transcribe →
  completeness check → LLM → TTS), barge-in wired via `on_wake`, first real latency numbers
  logged. [docs/history/A4.md](docs/history/A4.md)
- **A5** — TTS first-chunk trigger tightened (`buffered_stream`'s threshold loosened); LLM
  TTFT residual diagnosed (~300ms is a persistent Ollama-side floor, not our code) but not
  fully closed — see A5b under Next. [docs/history/A5.md](docs/history/A5.md)
- **A5a** — Persona (`config/persona.md`) written and verified live against real
  `gemma4:e4b` calls. Surfaced the two model-behavior findings under **Known model
  limitations** below — kept there verbatim, not just linked, since they're load-bearing
  for every later phase that asks the model to follow an instruction reliably.
  [docs/history/A5a-persona.md](docs/history/A5a-persona.md)
- **A6/A7** — Memory, hand-rolled (`services/memory/`), not Letta (investigated first;
  disqualified by per-turn round-trip cost, not dependency friction). Profile injection,
  rolling-context summarization at 70% fill, sqlite-vec retrieval. Verified across a real
  process restart — a fresh process answered from disk alone.
  [docs/history/A6-A7-memory.md](docs/history/A6-A7-memory.md)
- **A8** — Agent loop (`services/brain/agent.py`, ~170 lines) + read-only tools (`fetch_url`,
  `read_file`/`list_dir`, `web_search`). `think=true` (`[thinking].agent`) was the fix for
  unreliable tool-call chaining — 5/5 clean vs. 2/5 with thinking off.
  [docs/history/A8.md](docs/history/A8.md)
- **A9** — Write tools + confirmation gate + abort hotkey + no-credentials rule, all
  dispatcher-enforced code, never persona text (rule 4). `shell` runs inside `CortanaShell`,
  an isolated, automount-disabled WSL2 distro — filesystem-enforced, not a blocklist.
  [docs/history/A9.md](docs/history/A9.md)
- **A10** — `ask_user` as a real callable tool (speaks the question via TTS; the answer side
  is keyboard-only until voice reaches `agent.py`). One-question-per-turn cap enforced in the
  dispatcher, not trusted to the model. [docs/history/A10.md](docs/history/A10.md)
- **A11** — Proactive daemon (`services/daemon/daemon.py`): poll → quiet hours → rate limit →
  relevance filter → wait for playback → announce. Never loads a second model — same Ollama
  server as the conversation loop. Output is CLI-only for now (no path into the live voice
  loop yet). [docs/history/A11.md](docs/history/A11.md)
- **A12** — Control panel UI (`ui/`), rebuilt in TypeScript: frameless glass chrome, live log
  tailing via `fs.watch()` (not polling), working memory inspector (edit/delete, not just
  view). [docs/history/A12.md](docs/history/A12.md)
- **A13** — CAD training-data pipeline (`cad/verified/`, `scripts/cad_log.py`,
  `scripts/cad_synth.py`). Every accepted part is execute-and-verify gated — `add-part`
  actually runs and validates a script before it can land in the library.
  [docs/history/A13.md](docs/history/A13.md)
- **A14** — CAD generation with a verification loop (`tools/cad.py`). Geometric validation
  (`verify_solid()`, watertight + volume + STEP export + wall-thickness + feature-count) is
  ground truth; vision (`gemma3:12b`, not `e4b` — e4b's Modelfile doesn't template images at
  all) is a weak secondary signal with an explicit "cannot tell" option.
  [docs/history/A14.md](docs/history/A14.md)
- **A15** — The character (`ui/character.html`, `ui/src/character_*.ts`): Live2D placeholder
  rig, real-cursor gaze tracking (works across monitors), multi-monitor walking (instant jump
  across the bezel gap, not simulated continuity), lip sync driven by real TTS amplitude.
  [docs/history/A15.md](docs/history/A15.md)
- **A15 follow-ups** — holographic shader overlay (`[ui.hologram]`, one combined PIXI
  filter), rewritten as real glyph-rain data texture (not banded noise), window opacity
  matched to the control panel's `panel_opacity`, idle wandering suppressed by real
  listening/speaking state (`services/ears/listening_state.py`, new).
  [docs/history/A15-followups.md](docs/history/A15-followups.md)
- **A18** — Computer use (`tools/computer.py`): four resolution tiers in priority order (UI
  Automation → Playwright → CLI recipe → vision-as-absolute-last-resort), kill switch
  verified three ways including a real physical keypress, per-app allowlist enforced twice
  (JSON-schema enum + live foreground re-check). `find_file` collapsed a flaky multi-step
  search chain into one call (2/3 → 3/3 chaining reliability).
  [docs/history/A18.md](docs/history/A18.md)
- **A19** — Marketing pipeline (`services/marketing/`): Ghost Typer reels automation, hands
  off to the separate Ghosttyper-web repo's Remotion project via local subprocess (no network
  hop), rendered output stays in cortana's own `marketing_out/`, not that product repo.
  [docs/history/A19.md](docs/history/A19.md)
- **A20** — Attribution loop (`services/marketing/{attribution,report,feedback}.py`). Read
  side built and tested against Ghosttyper-web's real schema; the loop honestly can't close
  yet — UTM capture and view-count data don't exist in that repo at all yet (real, concrete,
  user-owned gaps, never faked here). [docs/history/A20.md](docs/history/A20.md)
- **A21** — Delegation (`services/workers/`): JSON-backed durable task queue, workers as real
  OS subprocesses (never a second resident model — same already-running Ollama server),
  specialization is the tool set, not the model. Live-verified: spawn is non-blocking and the
  foreground agent loop stays responsive while a worker renders, the concurrency cap holds
  under real contention, and the kill switch — after a real gap was found and fixed (see
  below) — takes down a worker's entire OS process tree (confirmed against 24 real PIDs
  including 8 live `chrome-headless-shell.exe`), not just the top-level PID.
  [docs/history/A21.md](docs/history/A21.md)
- **A22** — Grounding upgrade + screen awareness, all four steps done.
  Step 1 benchmarked GTA1-7B vs Holo2-8B on a real 33-target set built from this machine's
  own apps (not published ScreenSpot-Pro numbers) — GTA1-7B won decisively (81.8% vs
  51.5%), inverting the published ranking (Holo2 58.9% vs GTA1 50.1%), the clearest
  evidence yet for benchmarking on real screens. Along the way, found and fixed a real bug
  in `tools/_computer_uia.py`'s `resolve()` — it couldn't connect to VS Code/Chrome/Electron
  apps at all (not "picked the wrong process," `ProcessNotFoundError` outright), silently
  zeroing UIA coverage on exactly the multi-process apps that matter most; real baseline
  went from 39.4% to 75.8% after the fix, no model swap needed. `[models].vision_grounding
  = "gta1-7b"` now live in `tools/computer.py`'s resolution path (kept deliberately
  separate from `[models].vision`, which `tools/cad.py` still uses for a different task).
  Step 2 built narrow, on explicit instruction, after checking the real overlap surface
  first: of Step 1's 33 targets, 25 had both UIA and the grounder fire, and 21 of those
  were the grounder merely agreeing with an already-exact UIA answer (the other 4 were the
  grounder being wrong) — an always-run-both design would've added latency for no measured
  benefit, so arbitration (`tools/_computer_uia.py`'s new `find_candidates()` +
  `tools/_computer_setofmark.py`, new) fires only when UIA's exact-name lookup misses AND
  the miss is genuinely ambiguous. Live-verified against a real Explorer window on the
  exact case Step 1's own benchmark never exercised — a purely descriptive target
  ("the config settings") with no matching exact accessible name — correctly resolved via
  fuzzy candidate-finding + set-of-mark. Step 3 (`tools/_computer_verify.py`, new) wraps
  every click/type with a before/after snapshot — a UIA re-query for tree-resolved targets,
  a screenshot diff otherwise — logs the outcome alongside the resolution tier that
  produced the target (so a pattern of UIA-resolved actions failing their post-check would
  actually become visible, which nothing before this could show), and never retries blind
  on a mismatch — the detail is returned for a human or the calling agent to act on. Also
  fixed a real, separate bug found while wiring this in: `tools/_computer_vision.py`'s
  `resolve()` signature had already been rewritten (uncommitted, pre-dating this session)
  to a two-stage `(grounding_model, description_model, description, hwnd)` shape, but
  `tools/computer.py` was still calling it with the old two-argument shape — the
  pure-vision fallback tier was silently broken until this pass. Both Step 2 and Step 3
  were then live-verified on real actions, not just read-only checks: two real clicks
  through `computer.py`'s full `execute()` path (a folder-opening double-click, a no-op
  single-click) each correctly reported `changed`/`unchanged`. Step 2's own accuracy
  claim was checked against a real baseline, on explicit instruction, before trusting it:
  the same 12 descriptive targets run through the pure GTA1-7B grounder alone (no
  set-of-mark) scored 4/12 vs set-of-mark's 6/12 — set-of-mark wins and stays, though the
  win isn't uniform (VS Code 3/6 vs 0/6, but Chrome's sparser UI actually favored the pure
  grounder 4/6 vs 3/6, including the one target — a username — set-of-mark structurally
  can't attempt at all). Step 4 (`tools/screen.py`'s new `look_at_screen` tool) adds the
  read-only counterpart to `computer.py`: UIA-exact text first, a `[models].vision` call
  second for what UIA can't express, attribution baked into the returned string rather
  than trusted to the calling model's wording. `[tools.screen].excluded_windows` is a real
  privacy rail (checked before any capture) but ships empty — it does nothing until
  populated with real password-manager/banking window titles.
  [docs/history/A22.md](docs/history/A22.md)
- **A23** — Tool wave 1, the system layer: `notify`, `media_keys`, `clipboard_read`/
  `clipboard_write`, `process_list`, `window_list`, `search_content`, `transcribe_media`,
  `capability_list` (28 tools now registered total). Live-testing `notify` caught the exact
  failure shape flagged going in — a toast call returned cleanly with no exception, but a
  real screenshot showed nothing appeared; tested against a genuinely registered AUMID to
  rule out app-registration before finding the real cause in the registry
  (`ToastEnabled = 0` — Windows notifications disabled system-wide on this machine, left for
  the user to flip, not this session's call). `notify` and the daemon's new toast path both
  now report/fall back to the honest state instead of silently succeeding or dropping the
  message. `media_keys` verification took two real, live-caught bugs to get right, and the
  second one mattered more than the first: comparing *a* session's `PlaybackStatus`
  before/after without tracking *identity* reported a false clean round trip (a pause hit a
  real YouTube tab, a later "restore" key hit Spotify instead, caught by the user noticing
  the actual audio, not by anything checked). The first fix - re-checking one specific app's
  own session instead of "current" - was itself still wrong: on this machine a single
  `play_pause` was observed to change TWO sessions in opposite directions in one call (paused
  a playing YouTube tab, resumed an already-paused Spotify, together), and checking only one
  app reported that as a confident, correct single-app result, because the app it happened to
  check genuinely had changed. Fixed properly by diffing every session that existed before or
  after a key press, not one — `tools/_smtc.py`'s `all_sessions()` — reporting a single
  confirmed app only when exactly one session changed, and every affected app plainly when
  more than one did. Re-tested with an honest, un-engineered setup (YouTube playing, Spotify
  already paused) and no restore key sent afterward, cross-checked against an independent
  session dump: this run changed only one session, matching the tool's report exactly — the
  earlier two-session behavior wasn't reproduced on this trial, which is recorded as an open,
  unexplained characteristic of this tool on a multi-source machine, not as resolved. An
  `app` parameter that focuses a target window first was considered and rejected both times —
  SMTC routing tracks playback activity, not window focus, so it wouldn't work and would be a
  false promise. The result now names every app confirmed to have changed, or reports honest
  uncertainty — never a single guessed winner. Added `media_control` as the structural fix
  `media_keys` can't offer: SMTC exposes per-session `TryPlayAsync`/`TryPauseAsync`/
  `TrySkipNextAsync`/`TrySkipPreviousAsync` (`tools/_smtc.py`'s `control_session()`, invoked
  via a real temp `.ps1` file with `-AppId`/`-Action` as separate parameters, never
  interpolated into a `-Command` string) — a specific app id, no routing heuristic to be wrong
  about. Tested on the exact scenario that broke `media_keys` twice: YouTube paused, Spotify
  paused, resumed YouTube specifically — only that session changed, cross-checked against an
  independent session dump. `media_keys` stays the no-app-name tool; `media_control` is for
  when the app is known. `transcribe_media` added
  `Transcriber.transcribe_file()` to `services/ears/stt.py` and live-transcribed a real voice
  reference recording accurately. `capability_list` reports three states (available/
  gated-behind-confirmation/dormant-with-a-reason) sourced from the live 28-tool registry and
  each tool's own `is_available()`, never a hand-maintained list — per explicit instruction
  after the user flagged that a naive "what can you do" list would be actively misleading
  with half these tools dormant or gated. `TOOL_CATALOG.md`, referenced by `PLAN.md`/
  `PROMPTS.md`, still doesn't exist in the repo — flagged, not fabricated; the user said
  they'd add it separately. `[tools.screen].excluded_windows` (A22) is still empty.
  [docs/history/A23.md](docs/history/A23.md)
- **A24** — Tool wave 2, deliverables: `write_docx`, `write_xlsx`, `write_pptx`,
  `write_pdf`, `pdf_read`, `ocr`, `email_draft`, `copy`/`move`/`rename`/`delete` (12 tools;
  40 registered total). The four writers already existed on disk from a prior session's
  `save` commit (deps, config, and the files themselves) but were never wired into
  `services/brain/agent.py`'s dispatcher — found and closed that gap first, then built the
  remaining eight. No Office install on this machine, so every writer was verified
  externally instead of by eye: re-opened each output with the library that wrote it
  (`python-docx`/`openpyxl`/`python-pptx`) and diffed content, validated docx/xlsx/pptx as
  real zip containers, and checked `write_pdf` via this session's own `pdf_read` (reportlab
  has no reader) — 18/18 checks passed. `ocr` (`tools/_ocr.py`) is built and self-gating on
  Tesseract genuinely being callable (`pytesseract.get_tesseract_version()`, not just the
  package importing — confirmed live that the import alone succeeds with zero Tesseract
  installed) but not live-verified: Tesseract isn't installed on this machine
  (`winget install --id UB-Mannheim.TesseractOCR` to activate). Per explicit instruction,
  `ocr` never falls back to the vision model when unavailable — a confident wrong
  transcription from a tier already measured fabricating is worse than an honest gap.
  Wired into `tools/screen.py`'s `look_at_screen` as a third tier between UIA (exact,
  accessibility tree) and vision (interpretive, last resort) — OCR reads text baked into
  the pixels themselves (a screenshot-in-a-screenshot, a rendered PDF, a canvas UI) that
  UIA's tree can't see; vision's prompt now explicitly defers text transcription to
  UIA/OCR. `email_draft` reuses `tools/_outlook.py`'s existing non-launching
  `is_available()` and adds a new `get_application()` helper (`CreateItem()` lives on the
  Application object, not the namespace `calendar_read`/`email_read` already use) — only
  ever calls `.Save()`, no `.Send()` path exists in the module at all. Dormant right now
  (Outlook isn't running in this session), same as `calendar_read`/`email_read`.
  `copy`/`move`/`rename`/`delete` all resolve through `tools/_fs.py`'s existing
  `write_whitelist_dirs`, no second path check invented; `delete` uses `send2trash`, never
  `unlink`. Live-verified against real files in `deliverables/`, checking actual filesystem
  state, not the tools' own return strings — `delete`'s check needed a second look: the
  Windows recycle bin's `Shell.Application` namespace strips file extensions from display
  names, which false-failed the first version of that check (a bug in the check, not the
  tool) before confirming the file was genuinely recoverable, not permanently unlinked.
  11/11 checks passed. `capability_list`'s `_DORMANT_REASONS` extended for `email_draft`
  and `ocr`. [docs/history/A24.md](docs/history/A24.md)
- **A24 follow-up, same session** — Tesseract installed live (`winget ... --silent`, at the
  user's explicit override of the "you install system deps yourself" default), which
  surfaced two real bugs `is_available()` alone couldn't have caught: winget's silent mode
  doesn't add the binary to PATH (fixed in `tools/_ocr.py` with a fallback to the standard
  install location, verified by actually invoking it, not just checking the file exists —
  scoped to the tool, not a machine-wide PATH edit), and `extract_text()` assumed a prior
  `is_available()` call in the same process had already configured
  `pytesseract.pytesseract.tesseract_cmd` — true in the live agent loop, false the moment
  `ocr.execute()` was called standalone during this fix's own verification, which raised a
  raw `TesseractNotFoundError` instead of trying the fallback. Fixed by making both calls
  go through the same self-resolving `_resolve()`, no cross-call coupling on a global.
  Re-verified end to end through the real `ocr` tool and `look_at_screen` (not the raw
  helper) against a real screenshot of this session's own window, per explicit instruction
  ("clean synthetic rendering is the easy case") — found OCR is mostly right on real UI text
  but not exact (real word-boundary/spacing errors: "Runit" for "Run it", "cloneon" for
  "clone on"), a materially different and more honest claim than the earlier synthetic test
  supported. Corrected "exact" language to "recognition, not fabrication" across
  `tools/ocr.py`/`tools/screen.py`/`tools/_ocr.py` — OCR still never invents content the way
  vision can, but it isn't a lookup like UIA either, and the tool's own output now says so.
  `ocr.is_available()` is `True`; `capability_list` reports it available, not dormant.
- **A25** — Default-allow computer use: `[tools.computer.apps]` inverted from an allowlist
  (the `app` parameter's JSON-schema enum came from its keys — only `explorer` was ever
  configured) to default-allow — `app` is now a free-form process-name-like string, and that
  config table holds only optional per-app overrides (`open_command`, a non-obvious
  `match_process`, Playwright routing). The real protection was never the allowlist — it's
  the confirmation gate, kill switch, live foreground re-check, and A22's post-action
  verification, all unchanged. `[tools.screen].excluded_windows` (A22) moved to shared
  `[tools].excluded_windows` — one list, not two, after explicitly weighing the alternative:
  the category it protects (password managers, banking) has no case where read-only access
  should be allowed but driving it shouldn't. Enforced two ways: structurally, at
  enumeration time (`tools/_computer_uia.py`'s `find_top_level_hwnds()` now takes
  `excluded_titles` and never walks a matching window's UI Automation tree at all —
  threaded through `resolve()`/`find_candidates()`, and into `tools/_computer_playwright.py`'s
  `resolve()` per-PAGE, not just per-window, since a single Chrome window can hold many tabs
  and only the active one's title shows at the OS level); and live, immediately before every
  click/type, which is what actually protects the Playwright/vision tiers that don't go
  through the structural filter. **Exclusion is per-window, not per-app** — a real design
  property, not an incidental detail: excluding one window's title doesn't wall off an
  entire application, since a resolution that can't use the excluded window correctly falls
  through to any other, non-excluded window of the same process (confirmed live — see
  below). Anyone populating `[tools].excluded_windows` needs a term that matches every window
  of an app to wall off the whole thing, not just the one window seen when the list was
  written. Ships empty — same gap A22/A23 already flagged, not resolved here.
  Playwright activated (dormant since A18, gated on a debug-port Chrome existing — that part
  didn't change; whether to actually put one in front of it was a deliberately unmade scope
  decision until this session's explicit instruction). Added
  `[tools.computer.apps.chrome]` (`match_process`/`playwright.cdp_port = 9222`) so the tier
  can route. What launching Chrome with `--remote-debugging-port` exposes, stated plainly:
  the same authenticated profile, not a sandbox — every logged-in site, saved-password
  autofill, everything. Given the user the exact command for this machine (closes existing
  Chrome first, since Chrome silently ignores the flag otherwise); not run — a real, standing
  exposure the user turns on themselves, same as every other system-level install/config
  change in this project. `capability_list` gained a live-computed **Scope notes** section
  (`computer`/`look_at_screen`, both sharing the same default-allow-except shape) reading
  `[tools].excluded_windows` at call time rather than a static description that would drift
  the moment the list is actually populated — currently reports both as "EXCLUSION LIST IS
  EMPTY." `scripts/computer_stats.py` (new) reads `logs/computer.jsonl` and reports
  `resolved_via`/`verify_outcome` rates with config-driven warning thresholds
  (`[tools.computer.stats]`) — A22 Step 3 already logged this data every action, but nothing
  before this read it back and surfaced a rate; a real bug found running it against this
  session's own log (action='open' calls always log `resolved_via="cli"`, which was inflating
  the UIA-rate denominator and reporting a false 33% instead of the real 100% on click/type
  actions — fixed by excluding `cli` from that calculation). Live-tested on Notepad, never in
  the old allowlist: drove it through the real registered tool, verified the result via an
  independent UIA re-query (Notepad's own character-count status text — read `"42 characters"`,
  exactly the typed marker's length) rather than the tool's own return string. A real mistake
  happened during cleanup, reported plainly rather than glossed over: killed a Notepad process
  by PID intending to close only this session's own disposable test window, but modern Windows
  11 Notepad can share one underlying process across multiple windows (the tabbed
  single-instance model), and the kill took down a second, pre-existing, unrelated Notepad
  window too (`errors.log`, opened before this session, not touched by any of this session's
  actual typing). Investigated rather than assumed-fine: found the file via Windows' own
  Recent Items shortcuts, confirmed its on-disk mtime predated this session by weeks and its
  size was consistent with the character count read earlier in the same session — nothing was
  lost, but the mistake (wrong assumption about process/window ownership) was real.
  [docs/history/A25.md](docs/history/A25.md)
- **A26** — FreeCAD scripting: a `freecad` tool that sends Python to an already-running
  FreeCAD GUI instance via a small, self-owned XML-RPC bridge
  (`scripts/freecad_rpc_bootstrap.py`, pasted into FreeCAD's own Python console once per
  session; `tools/_freecad.py` client + `tools/freecad.py`). Transport decision, asked for
  explicitly: FreeCAD exposes a console command file (no live-execution trigger for an
  already-open GUI exists), `import FreeCAD` from an external process (a separate,
  disconnected session — never the open GUI), or a socket/RPC server started from inside the
  running process (the only one that satisfies "already-running instance executes code and
  shows the result live"). Picked the third, built self-owned rather than depending on an
  unverifiable third-party FreeCAD RPC addon — same "own small transport code end to end"
  precedent as A23's `media_control`. What it can't do, stated plainly: doesn't survive
  closing FreeCAD (re-run the bootstrap after every launch), localhost only, and FreeCAD's Qt
  GUI isn't thread-safe from the RPC server's own listener thread — handled with a
  QTimer-polled queue marshaling every call onto FreeCAD's main thread, not decoration.
  `render_part` loads an already-generated, already-verified part into the live document via
  FreeCAD's own `Part.insert()` STEP importer — deliberately not regenerated through
  FreeCAD's native Part/Sketcher API, which would be a second geometry path that could
  diverge from what `verify_solid()` (A14) actually measured; reuses
  `tools/_cad_export_common.py`'s lookup/export, the same code `export_step.py` already
  uses. `run_python` sends arbitrary code through, confirmation-gated like `tools/shell.py`;
  `render_part` isn't, no more consequential than `export_step.py`'s own unconfirmed write.
  FreeCAD isn't installed on this machine — built real and self-gating per explicit
  instruction (`winget install --id FreeCAD.FreeCAD`), same standard A24 held OCR to before
  Tesseract landed. Everything genuinely testable without FreeCAD was tested live: a real
  stand-in XML-RPC server (plain Python, same `ping()`/`run_code()` protocol) exercised the
  actual registered tool end to end and caught a real bug — the generated snippet's first
  draft opened with `import FreeCAD, Part`, redundant against the pre-seeded exec()
  namespace both the bootstrap and the stand-in already provide, and it broke outright
  against the stand-in (no real `FreeCAD` module on this machine's path) — fixed by trusting
  the documented pre-seeded-namespace contract instead. `render_part` against the real,
  already-verified `cad/verified/bracket` part was checked against actual recorded calls
  from a behaviorally-real fake (not just "the snippet parses"), and the resulting STEP file
  was re-imported independently via cadquery's own importer and confirmed to have positive
  volume — a real solid, not corrupt. 10/10 checks passed. What's genuinely unverified,
  stated plainly: the bootstrap script's own FreeCAD-specific calls (`Part.insert()`,
  `FreeCADGui.activeView()`, the actual Qt threading behavior) — a best-effort first draft
  against documented FreeCAD/Qt constraints, not a live-verified one.
  [docs/history/A26.md](docs/history/A26.md)
- **A27** — Crawl4AI: `fetch_url` gained a config-selectable backend
  (`[tools.fetch_url].backend`) — `trafilatura` (unchanged), `crawl4ai`
  (`tools/_crawl4ai.py`, a real headless browser via Playwright), or `auto` (the default:
  trafilatura first, Crawl4AI only as a fallback when the result comes back empty or under
  `auto_fallback_min_chars`, 200). Dependency pins checked before building against it, per
  explicit instruction: `uv add crawl4ai` was purely additive (one new line in
  `pyproject.toml`), numpy stayed at 2.4.6 (pin `<2.5`), transformers at 5.0.0 (pin `<5.1`),
  torch/CUDA unaffected — verified directly, not assumed. Measured on real pages before
  picking a default, also per explicit instruction: on two static pages both backends
  extracted correctly, but trafilatura was both faster (0.2–1s vs 1–4s) **and** cleaner
  (Crawl4AI's markdown carried more boilerplate even with `fit_markdown`'s
  `PruningContentFilter`). On the case that actually matters — a page whose content
  renders client-side (`quotes.toscrape.com/js/`) — trafilatura returned 29 chars of loading
  shell (reads as a successful 200, missing the real content entirely); Crawl4AI returned
  1663 correct chars in ~1s. That gap is what `auto` mode exploits: a thin trafilatura
  result is itself the signal a page needs real JS rendering, so browser cost is paid only
  where it's actually needed. `tools/_crawl4ai.py` launches a fresh browser per call rather
  than holding one open — a deliberate exception to rule 7's usual persistent-instance
  guidance, since a standing browser is real cost for an occasionally-called tool.
  `logs/fetch_url.jsonl` (new) records which backend actually ran and whether a fallback
  fired, same instrumentation discipline as A25's `computer_stats.py`. Live-tested against
  the real, registered tool and verified externally — not the tool's own return: both the
  static and JS-rendered fetches were checked against the real, known quote text on each
  page, and the JS-page log confirmed the fallback genuinely fired
  (`fallback_from_trafilatura_chars: 29`) and genuinely helped, not just that both backends
  happened to return something non-empty. 5/5 checks passed. This closes out Track A.
  [docs/history/A27.md](docs/history/A27.md)
- **UI craft pass (ad hoc, not a PROMPTS.md phase)** — installed emilkowalski/skills
  (`emil-design-eng`, `apple-design`, `animate`, and 7 more under `.agents/skills/`;
  needed Node 22 via the existing nvm-windows install, invoked directly rather than
  switching the machine's global active Node version) and used them for a craft pass on
  the Electron control panel (`ui/index.html`/`style.css`/`renderer.ts`) — typography
  (tabular-nums on every numeric readout, size-correct letter-spacing), a real spacing
  scale, legibility-contrast bumps on muted text over the translucent/blurred background,
  and motion scoped strictly to transitions/state changes (a sliding tab indicator,
  button press feedback, badge state color transitions, a real collapse-out on memory
  entry deletion) — explicitly never the log feed (appends many times a second) or the
  latency numbers (update every turn), per direct instruction. Character layer
  (`character.html`/`character_renderer.ts`/`character_hologram.ts`/`character_main.ts`/
  `[ui.hologram]`) untouched, confirmed by diff. Checked constraint #3's premise
  ("the panel now surfaces computer_stats/capability_list/worker status") against the
  actual code before designing around it — `git log -- ui/` showed nothing touched since
  A18, and none of those three are wired into the renderer/main/preload/types at all. The
  user cut scope to the four existing tabs in response, explicitly as an **open design
  question** (do those three belong in this panel at all, and if so how) rather than a
  deferred task. Verified live: clean TypeScript build, drove the real app through all
  three non-default tabs via `tools/computer.py`'s real UIA click (not mocked) and
  screenshotted each. Found a real bug in the screenshot tooling itself, not the UI: on
  this dual-monitor layout (a monitor left of the primary, negative win32 coordinates in
  that region), cropping `PIL.ImageGrab.grab(all_screens=True)` with raw
  `GetWindowRect()` values silently grabbed a different window (Spotify) instead of
  erroring — fixed by subtracting `GetSystemMetrics(SM_XVIRTUALSCREEN/YVIRTUALSCREEN)`
  before cropping. [docs/history/ui-craft-pass.md](docs/history/ui-craft-pass.md)

**Next**: A16/A17 (camera/ambient awareness) are the next fully-untouched phase
after A22/A23 wrap. A5b
(latency, specifically the LLM TTFT residual) is still open but deliberately
paused, not abandoned - re-run `latency_report.py` after a real live session to
get actual `load_duration`/`prompt_eval_duration` numbers for genuine live-pipeline
calls (not isolated reproductions) and close the remaining TTFT gap. A listening
verdict is pending on two independent items: whether `1sentence` (118ms gap, faster
than the landed `1sentence_or_150chars`) is worth it, and whether the
length-correlated pacing anomaly (`voice_refs/audition/chunk_length_accent/`) is
the source of the reported accent drift - if confirmed, extending
`backchannel_pool.py`'s duration-sanity retry to the main response path is the
likely fix, not yet built since it wasn't this session's call to make unilaterally.
`°C`/`F` and mixed-alnum unit normalization gaps noted above, not yet fixed.

## Known model limitations

Two measured findings about `gemma4:e4b`'s behavior, load-bearing across every phase that
asks the model to follow an instruction reliably — kept here verbatim rather than only in
`docs/history/`, since later phases (A10's ask_user cap, A18's find_file fix) cite them
directly.

**Dry wit doesn't reliably fire.** Tested two different ways — a repeated-question scenario
(meta-awareness of being asked N times) across five conditions (full persona, a stripped
third-size persona, positive examples in the shape rules, a literal third-ask matching the
sample line's own framing, and `gemma4:12b` instead of `e4b`), 25 runs, zero firings in any
condition — and a content-only scenario (the dry aside is about the answer's content, not the
exchange — four factual questions x5 runs, full persona, text-only), zero firings there too.
Ruling out both prompt structure and model size means this isn't a test artifact — the trait
is genuinely close to absent under this persona/model combination. Not chasing it further.
One incidental counter-data-point: 2 of 20 responses in the padding retest below landed a
genuine dry aside unprompted — so the trait *can* fire, just rarely, and not reliably from any
rule change tried so far. Full methodology: [docs/history/A5a-persona.md](docs/history/A5a-persona.md).

**Instruction-driven behavior compliance caps at roughly two-thirds, regardless of how the
rule is worded — and the fix is moving the task below the model's reasoning ceiling, not
another wording pass.** First measured on unsolicited padding (A5a): a negative-prohibition
rule ("she answers only what was asked") measured 10/15 responses volunteering unrequested
checks/warnings/next-steps; rewriting it as a positive definition measured 9/15 on an
identical retest — flat, within noise, not a fix. Two different rule phrasings converging on
the same rate is real evidence the lever isn't persona wording. Confirmed independently on a
completely different task (A18): a multi-step "search before asking" tool-chaining fix,
after the clearest wording found, still only succeeded 2/3 of trials — "which lines up almost
exactly with A5a's independently-measured ceiling for other persona/instruction-driven
behaviors on this model." The fix that actually worked there wasn't a fourth wording pass —
it was `find_file`, a new tool that removed the multi-step reasoning from the model's job
entirely (one call instead of a chained `list_dir` sequence it could bail out of early),
which took the same test from 2/3 to 3/3. **The general lesson: when a model behavior sits at
this ~2/3 ceiling, stop rewording the instruction and ask whether the task itself can be
made to need fewer reasoning steps.** Full methodology:
[docs/history/A5a-persona.md](docs/history/A5a-persona.md),
[docs/history/A18.md](docs/history/A18.md).

## Architecture

Separate services behind local HTTP/socket interfaces. Not a monolith.

```
cortana/
├── services/
│   ├── ears/     # wake word, VAD, STT
│   ├── brain/    # LLM orchestration, agent loop
│   ├── voice/    # TTS
│   ├── memory/   # Letta wrapper + profile
│   └── daemon/   # proactive trigger watcher
├── tools/        # one file per agent-callable tool
├── ui/           # Electron: control panel + character
├── config/
│   ├── cortana.toml   # ALL tunables. No magic numbers in code.
│   ├── profile.md     # durable facts, hand-editable
│   └── persona.md     # character brief
├── cad/verified/ # CAD training data (start immediately)
├── logs/
└── scripts/      # setup, benchmarks
```

## Non-negotiable rules

1. **Stream everything.** Audio into STT, tokens out of the LLM, sentences into TTS. Never
   wait for a complete result at any stage.
2. **Config over constants.** Any threshold, model name, path, or timeout goes in
   `cortana.toml`. If you're about to hardcode a number, don't.
3. **Instrument before you optimize.** Every stage logs its own latency. No performance
   work without measurement.
4. **Read tools are free, write tools are gated.** Anything that deletes, sends, spends,
   submits, or unlocks requires explicit confirmation. Never handle passwords.
5. **Small commits.** Commit whenever something demonstrably works. Never batch.
6. **Verify before declaring done.** Run it. For visual output, render and look at it.
   For audio/TTS output, transcribe it and check the text, not just duration or a
   duration-ratio proxy — the char-limit truncation investigation (see A3 Step 3 below)
   found a real, consistent truncation (XTTS dropping the last word past ~450 chars)
   that duration ratios alone (91-113% of baseline) did not clearly reveal; only
   transcribing the actual audio surfaced it. Any future TTS change (new strategy,
   engine, param change) gets a transcript check as part of verification, not just a
   listen-and-duration check.
7. **One persistent client/model instance per process, explicit close.** Any service
   making repeated calls (Ollama, TTS models, APIs) opens its client/model once at
   process/module level and reuses it for the process lifetime, with an explicit
   close(). A fresh instance per call costs real setup latency — a new httpx client
   per call cost ~280ms in connection setup alone (services/brain/client.py); a
   reloaded TTS model costs seconds (services/voice/tts.py).
8. **Torch slow? Check `torch.cuda.is_available()` before profiling anything else.**
   `uv add` pulls CPU-only torch wheels from PyPI by default on Windows — no error,
   no warning, just silent fallback to CPU. This project has now hit three silent
   misconfigurations that never crashed, just underperformed (thinking mode inflating
   TTFC, per-call httpx client, CPU-only torch). Assume a new one is a config problem
   before assuming it's a hard performance ceiling.
9. **Not every file in this repo is safe to replace wholesale.** `PLAN.md` and
   `PROMPTS.md` are reference docs maintained outside the repo — dropping in an
   updated copy is expected and fine. `CLAUDE.md`, `config/cortana.toml`, and
   `config/persona.md` are live files edited during the build itself; overwriting
   one from an external copy silently discards everything learned since that copy
   was made, with no error and no diff that looks obviously wrong at a glance. This
   has already happened twice — rule 7 was lost once, the entire Done log another
   time. If one of these three looks collapsed, reverted, or missing content you
   know should be there, say so before working from it — don't assume it's current.
10. **A tool's `is_available()` check must be incapable of starting or changing
    anything.** It runs on every single `run_agent()` call (`services/brain/
    agent.py`'s `_drop_unavailable_tools()`), for every gated tool, whether or
    not the model ends up using it — so it has to be cheap, read-only, and
    side-effect-free, every time, unconditionally. Found the hard way in A9:
    `tools/_outlook.py`'s first version checked availability via
    `win32com.client.Dispatch("Outlook.Application")`, which launches Outlook
    if it isn't already running — confirmed live, it spawned a real process
    that hung for 30+ seconds with no visible window and relaunched itself
    once after being killed. Wired into the live loop, that check would have
    silently started a real Outlook process on every single conversational
    turn. Fixed by checking for an already-running process first (read-only,
    no COM), then `GetActiveObject` (not `Dispatch`) - it only ever attaches
    to something already running, never launches. If a capability can't be
    checked without a launch/side-effect, the tool stays permanently dormant -
    that's the correct failure mode, not a workaround to avoid.

## Latency budget (Phase 1, enforced in code)

Rewritten in A5 once real component costs were known (2026-08-05) - the original
table below was a set of guesses made before any of this was measured, and the
~1.15s total turned out to be physically unreachable (VAD's floor alone is 610ms,
LLM TTFT's floor is another ~300ms - the two floors alone exceed the old total's
budget for STT and TTS combined). Two stages are **structural floors**: fixed
costs from a deliberate design decision or an external system, not moveable by
anything on our side without reopening that decision. The rest are
**controllable**, with a realistic target based on what's actually been measured,
not what sounded reasonable in advance.

| Stage | Type | Floor | Controllable target | Realistic target | Currently measured |
|---|---|---|---|---|---|
| Wake word detect | controllable | — | ~10ms | **~10ms** | 1.3-2.4ms median (OK) |
| VAD endpoint | **fixed floor** | 610ms | none | **610ms** | 610.0ms median, both live sessions (exactly at floor) |
| STT | controllable | — | ~350-400ms | **~375ms** | 313-538ms median |
| LLM time-to-first-token | **floor + controllable** | ~300ms | +~50-150ms | **~350-450ms** | 681-811ms median (**gap not closed**) |
| TTS engine synthesis (first chunk) | controllable | — | ~450-500ms | **~475ms** | ~510-540ms median (n=79 across two measurement methods) |
| **First audio out (derived)** | | | | **~1.9s** | **~2.3s** |

Why each floor is a floor, not a target to chase:
- **VAD endpoint (610ms)**: `[audio.vad].min_silence_duration_ms=600` was deliberately
  raised from 300ms after `scripts/vad_pause_test.py` measured real mid-sentence
  hesitation gaps at 582-1822ms - every threshold in the 300-500ms range clipped
  real speech identically. Backchannel prompting (`services/ears/backchannel*.py`)
  is the accepted mitigation for the *cost* of this floor, not a way to shrink the
  stage itself. Don't re-tune this value; that door is closed (see the A2/A3
  entries above for the full investigation).
- **LLM TTFT (~300ms of it)**: Ollama's own `load_duration` (now logged by
  `client.py`) measured ~285-335ms on *every* call tested this session - warm or
  cold, `/api/chat` or `/api/generate`. Not a cold-start signal despite the name;
  a persistent per-call floor on this Ollama/model/GPU setup, confirmed not
  reducible via endpoint choice (already the `/api/chat` vs `/api/generate`
  question A1 and A5 both independently ruled out as noise). The controllable
  remainder is `prompt_eval_duration` (context-size-dependent, measured 30-266ms
  in isolated tests) - that part scales with conversation length and is worth
  revisiting if/when A6's rolling-context summarization ships.

The other three stages (STT, TTS engine synthesis, and the controllable slice of
LLM TTFT) don't have an externally-imposed floor the way VAD and Ollama's
load_duration do - "realistic target" there means "close to what's already
measured for the current model/config choice," not a theoretical minimum. STT
could go lower with a smaller Whisper model (`small` measured faster on one clean
test in the VRAM investigation, see A3) but that's an accuracy trade-off never
validated, not a free win. TTS engine synthesis hasn't had a dedicated
optimization pass yet - DeepSpeed was checked and is blocked on a full CUDA
Toolkit install this machine doesn't have (see A3's XTTS streaming work).

**First audio out, properly derived**: the old table summed "LLM time-to-first-token"
and "TTS first chunk" as if they were sequential, but they aren't - the old
`ttfc_ms` was measured from `speak_stream()`'s entry (effectively the same moment
the LLM call starts), so it already *contains* the LLM TTFT wait inside it, not
adds to it. Summing both double-counted that wait. The corrected total is Wake +
VAD + STT + LLM TTFT + **TTS engine synthesis only** (the genuinely TTS-specific
cost, isolated via `tts.py`'s `synthesize_call` records and `latency_report.py`'s
`_split_ttfc()` - see the A5 entries above): realistic ≈ 10 + 610 + 375 + 400 + 475
= **~1.87s**. Current measured, same non-double-counting structure ≈ 2 + 610 + 425
+ 745 + 525 = **~2.3s**.

**The honest picture**: ~1.9s is the real achievable floor on this hardware/model
stack as currently chosen - not ~1.15s, which was never reachable once VAD's and
Ollama's floors are accounted for. We're at ~2.3s, a ~400ms gap - and that gap
maps almost entirely onto the LLM TTFT residual (681-811ms measured vs. a
~350-450ms realistic target, roughly a ~330-360ms unexplained difference) that
A5's diagnosis session didn't close (persona prompt, history growth, and
endpoint choice were all ruled out; concurrent GPU load only accounts for
~35-40ms of it). Closing that gap - not further work on any other stage - is what
gets first-audio-out to the real floor. Log every stage every turn; re-run
`scripts/latency_report.py` after a real live session (its `--since`/`--until`
scope to one window, and it now reports Ollama's `load_duration_ms`/
`prompt_eval_duration_ms` breakdown directly once real calls carry that data) to
verify the ~1.9s floor is actually reachable once the TTFT gap is closed.

## Conventions

- Python for services, TypeScript for UI
- `uv` for Python deps
- Everything ASCII in identifiers — `cortana`, never stylized forms
- Structured logging (JSON lines) to `logs/`, one file per service
- No secrets in the repo; `.env` gitignored

## Working style

- One step at a time from `PROMPTS.md`. Don't jump ahead or bundle steps.
- If a step is ambiguous, ask before building.
- Tell me what you're NOT doing when you scope something down.
- If something in `PLAN.md` seems wrong given what you've found, say so rather than
  silently working around it.
