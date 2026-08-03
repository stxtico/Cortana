# CORTANA — Step prompts

One step per session (or per clear checkpoint). Copy the prompt, verify the "done when,"
commit, update `CLAUDE.md`, move on.

**Track A** runs on the RTX 3080 Ti today. It is the whole build.
**Track B** needs the Spark. It is upgrades to a system that already works.

Don't skip ahead. Don't bundle two steps into one prompt.

---

# TRACK A — Build now (3080 Ti, 12GB)

## A0 — Skeleton and benchmark

> Set up the CORTANA repo skeleton exactly as described in CLAUDE.md — the services/,
> tools/, ui/, config/, cad/, logs/, scripts/ tree with `__init__.py` stubs, a `uv`
> project, and a `.gitignore` covering `.env`, `logs/`, `out/`, `node_modules/`.
>
> Create `config/cortana.toml` with a `[models]` section (fast/primary/heavy/alt keys,
> only `primary` populated for now with `gemma-4-12b`) and empty `[audio]`, `[latency]`,
> `[memory]` sections.
>
> Then write `scripts/bench.py`: measures time-to-first-token and tokens/sec against a
> local Ollama model at 1K, 8K, and 32K context, three runs each, prints a markdown table
> and writes `logs/bench-<date>.json`.
>
> Don't build any services yet. Just the skeleton and the benchmark.

**Done when:** `uv run scripts/bench.py` prints a table. Save the output — you'll compare
against it for the rest of the project.

---

## A1 — Streaming LLM client

> Build `services/brain/client.py`: a thin async Ollama client that streams tokens.
>
> Requirements: reads model name and endpoint from `cortana.toml`; exposes
> `async def stream(messages, tools=None, think: bool = False) -> AsyncIterator[str]`;
> supports OpenAI-format tool calling; logs time-to-first-token and total duration as JSON
> lines to `logs/brain.jsonl` on every call.
>
> `think` is a per-call argument, not a global toggle — `[thinking]` in `cortana.toml` holds
> the default per use case (`conversational = false`, `cad = true`, `heavy = true`) but the
> call site decides every time, so A14 (CAD) and B3 (heavy mode) can turn it on without
> touching the voice loop.
>
> Gotcha already hit once in `scripts/bench.py`: with `think` unset, Ollama streams hidden
> reasoning through a separate `"thinking"` field while `"response"` stays empty until
> reasoning finishes, then flushes the final answer as one chunk. TTFT must be measured off
> the first non-empty `response` OR `thinking` chunk, whichever arrives first — otherwise
> you silently measure "reasoning done," not first token out.
>
> No agent loop yet, no tools yet. Just streaming plus instrumentation. Include a
> `__main__` block I can run to send one prompt and watch tokens arrive.

**Done when:** you run it, tokens appear progressively, and `logs/brain.jsonl` has a TTFT
number.

---

## A2 — Ears: wake word, VAD, STT

> Build `services/ears/`. Three pieces, one file each:
>
> - `wake.py` — openWakeWord listening for "hey cortana". Threshold from config. Emits an
>   event, does not block.
> - `vad.py` — silero-vad for endpoint detection. Never fixed-duration recording.
> - `stt.py` — faster-whisper `large-v3-turbo`, transcribes VAD-segmented chunks.
>
> Wire them into `services/ears/pipeline.py` exposing an async generator yielding
> transcribed utterances. Log per-stage latency (wake detect, VAD endpoint decision, STT)
> to `logs/ears.jsonl`.
>
> Include a `__main__` that prints transcriptions live so I can test with a mic.

**Done when:** you say "hey cortana, what time is it" and the transcript appears with all
three latencies logged.

---

## A3 — Voice: streaming TTS

> Build `services/voice/tts.py` using Kokoro TTS.
>
> The critical requirement: `async def speak_stream(token_iterator)` buffers incoming
> tokens until a sentence boundary, then immediately synthesizes and plays that sentence
> while more tokens are still arriving. Do not wait for the full text.
>
> Also add `sanitize()` that strips markdown, code fences, list markers, and URLs before
> synthesis — spoken output must be plain prose.
>
> Log time-to-first-audio-chunk.

**Done when:** you pipe a slow token stream in and audio starts before generation
finishes.

---

## A4 — Close the loop

> Wire ears → brain → voice into `services/brain/loop.py`. Full path: wake word →
> transcribe → stream to LLM → stream sentences to TTS.
>
> Add `config/persona.md` with a starter character brief (traits, plus a hard rule: two
> sentences max unless asked for more) and load it as the system prompt.
>
> Add barge-in: if speech is detected while TTS is playing, kill playback immediately and
> start listening.
>
> Add `scripts/latency_report.py` that reads all three log files and prints the per-stage
> budget table from CLAUDE.md with actual vs. target.

**Done when:** three conversational turns in a row without it feeling like waiting on a
machine. Run the latency report and note which stages miss budget.

---

## A5 — Latency tuning

> Here's my latency report: [paste output].
>
> Work through the stages that miss budget, biggest gap first. For each: diagnose, propose
> the fix, implement, re-measure. Don't add any features during this step.

**Done when:** first audio out is consistently under ~1.5s. This step may take several
sessions. It is the most important one in Track A.

---

## A6 — Memory

> Integrate Letta as the memory layer in `services/memory/`.
>
> Three layers, all required: (1) `config/profile.md` injected into every turn, never
> retrieved; (2) rolling context — at 70% window fill, summarize the oldest chunk, keep
> the summary, push raw text to storage; (3) vector retrieval of the top 5-10 relevant
> past fragments per turn.
>
> Use the fast model for summarization so it doesn't block the main loop. Since we only
> have one model loaded right now, run summarization asynchronously after the turn
> completes, not during.

**Done when:** you restart the process, reference something from an earlier session, and
it recalls correctly.

---

## A7 — Memory inspector

> Add a CLI at `scripts/memory.py`: list stored facts, show what was learned from which
> session, delete an entry, edit `profile.md` safely.
>
> Do this now, not later — uncorrected memory drift is hard to untangle after months.

**Done when:** you can see everything it believes about you and correct a wrong entry.

---

## A8 — Agent loop and read-only tools

> Build the agent loop in `services/brain/agent.py`. Roughly 200 lines: tool list → model
> picks → execute → feed result back → repeat. Max 10 iterations, hard cap. Do not use
> LangChain.
>
> Then build these tools in `tools/`, read-only only:
> `web_search` (Tavily), `fetch_url` (httpx + trafilatura), `read_file` and `list_dir`
> (whitelist of directories from config).
>
> Every tool call logged with arguments and result. Tool dispatch keyed on active model so
> we can scope permissions later.

**Done when:** "what's the weather in Miami" triggers a search and she answers from it.

---

## A9 — Write tools with confirmation gates

> Add write-capable tools: `write_file`, `shell` (Docker container, command whitelist from
> config), and calendar/email read.
>
> Every one goes behind a confirmation gate: she states what she's about to do, waits for
> spoken confirmation, then acts. Gate logic lives in the dispatcher, not the prompt.
>
> Add a global abort hotkey that halts any in-progress tool execution.

**Done when:** she asks before writing a file, and the abort hotkey stops her mid-action.

---

## A10 — Clarifying behavior

> Add `ask_user` as a real callable tool — arguments `question` and optional `options`,
> returned through TTS, answered by voice.
>
> Update `persona.md`: for anything irreversible, any missing dimension or filename, or
> any genuinely ambiguous request, calling `ask_user` is correct and guessing is a failure.
> Cap at one clarifying question per turn, two per task.
>
> When she does proceed on an assumption, she states it in one clause.

**Done when:** she asks you something you hadn't thought to specify.

---

## A11 — Proactive daemon

> Build `services/daemon/`. Separate process from the conversation loop, watching exactly
> three triggers: calendar event in 20 minutes, email matching a config rule, voice-set
> timers.
>
> Then the part that decides success — the relevance filter: route every candidate through
> the model with "is this worth interrupting for, right now?", plus quiet hours and a rate
> limit (max N per hour, from config).
>
> Default the rate limit low. Too quiet is recoverable; too noisy gets it turned off.

**Done when:** it tells you something useful unprompted and stays quiet the rest of the
day.

---

## A12 — Control panel UI

> Build `ui/` as an Electron app: conversation history, live tool-call indicator,
> per-stage latency readout, memory inspector tab, and a big visible indicator of which
> model is active.
>
> Utility window, not the character. Function over polish — this is where you'll debug
> everything else from.

**Done when:** you can watch a full turn happen with latencies updating live.

---

## A13 — CAD data pipeline (no model required)

> Set up `cad/verified/` and the logging pipeline described in PLAN.md. Each part gets
> `part.py`, `description.md`, `attempts.jsonl`, `meta.json`.
>
> Build `scripts/cad_log.py` to add a part and append failed attempts, and
> `scripts/cad_synth.py` that takes a verified parametric script, generates N dimensional
> variants, executes each in CadQuery, discards failures, and writes verified pairs to
> `cad/dataset.jsonl`.
>
> This is pure plumbing — no model involved. Do it before modeling your first real part.

**Done when:** one hand-written bracket produces 100+ verified synthetic pairs.

---

## A14 — CAD generation with verification loop

> Build `tools/cad.py`: generate CadQuery from a text description, execute it, render PNGs
> from 3 angles, feed renders back to the vision model, revise. Max 5 iterations.
>
> Between iterations, run geometric validation: executes cleanly, watertight, no
> zero-thickness walls, no negative clearances, dimensions match what was stated.
>
> Retrieve the 3 most similar parts from `cad/verified/` as few-shot examples on every
> generation.
>
> Add `export_step` and `export_stl`. Add unit checking — flag any bare number and any
> mixed-system arithmetic.

**Done when:** you describe a simple bracket, answer her dimension questions, and get a
STEP file that slices.

---

## A15 — The character

> Build the character layer in `ui/`: transparent frameless always-on-top window,
> `setIgnoreMouseEvents(true, {forward:true})` by default, Live2D rig (use a free
> placeholder model for now).
>
> State machine: idle, listening, thinking, speaking, walking, working. Emotion as a
> separate overlay layer. Lip sync driven by TTS audio amplitude.
>
> Multi-monitor: use Electron's `screen` API, walk between my two 1440p displays by
> animating window position. Handle display hot-plug so she can't get stranded offscreen.
>
> Add gaze tracking toward the active window — do this one early, it's the highest
> presence-per-effort feature here.

**Done when:** she's on screen, follows your cursor with her eyes, and can walk to the
second monitor.

---

## A16 — Camera and ambient awareness

> Build `services/eyes/`. Tiered pipeline — cheap layer always on, VLM on trigger only:
>
> - Always: MediaPipe face detection, head pose, gaze direction. Produces rolling state
>   (present/absent, looking at screen/away/down, seated duration).
> - On meaningful state change AND only when something's worth saying: one frame to the
>   VLM.
>
> Build on attention and presence, NOT emotion inference. No statements about how I feel.
>
> Hard rules: frames never written to disk, only derived state to logs. Drop ambient
> commentary entirely when a second face is detected. Route through the Phase 4 relevance
> filter with a tighter budget — roughly one observation per hour.

**Done when:** it's made one accurate, useful observation about your working patterns and
otherwise stayed quiet.

---

## A17 — Camera-cover reaction

> Add cover detection to `services/eyes/`: near-zero luminance AND near-zero pixel
> variance = covered; low luminance with variance = dark room; no stream = disabled.
> Debounce 1.5s. Fire on both cover and uncover.
>
> Then the reaction system: a background job generates fresh lines in her voice with
> today's context and pre-renders the TTS into a pool. Lines expire on use, never repeat
> in a session. If the pool is empty, generate live and split the reaction — animation
> fires at 0ms, audio trails.
>
> Escalation tiers by daily count. Fire roughly one time in three.

**Done when:** you cover the camera, she folds her arms instantly, and says something you
didn't write.

---

## A18 — Computer use

> Build `tools/computer.py`. Resolve targets through the accessibility tree (Windows UI
> Automation), Playwright for browsers, CLI where it exists. Vision + coordinates only as
> last resort.
>
> **Build the global kill-switch hotkey first and test it before writing a single click.**
>
> Then the performance layer: character walks to the target region, plays a reach
> animation, cursor moves along an eased path, brief pause, then click. The pacing is
> deliberate — it's what lets me see what she's about to do and abort.
>
> Per-application allowlist from config. Spoken confirmation for anything that sends,
> deletes, purchases, or submits. Never type a password.

**Done when:** "open the CAD folder and pull up the bracket" works, and you can stop her
mid-motion with one key.

---

## A19 — Marketing pipeline

> Build `services/marketing/` for the Ghost Typer reels pipeline. Reference the
> ghost-typer-reels skill for content craft rules.
>
> Stages: brief generation (rotate angle × doc type × audience, enforce variety), script
> generation, format assignment (no format repeats within N posts), then hand off to the
> existing Remotion project for rendering.
>
> Add automated still verification: render the payoff frame and the longest-text frame,
> feed both to the vision model, check specifically for text overflow, dark/inverted logo,
> off-centre numbers. Then `ffprobe` assert 1080,1920,yuv420p.
>
> Queue to `content_posts` with a per-video UTM-tagged link.

**Done when:** one command produces a verified batch of MP4s awaiting approval.

---

## A20 — Attribution loop

> Wire the conversion feedback: per-video UTM → landing → trial signup → paid conversion,
> using the existing Supabase tables.
>
> Build a report ranking hooks, formats, angles, and document types by conversions per
> thousand views. Then feed the top performers back into the brief generation prompt as
> examples.

**Done when:** you can name your best-converting format with data behind it.

---

# TRACK B — Requires the Spark

Everything above already works. These are upgrades.

## B0 — Migrate and re-benchmark

> Set up the Spark: Ollama, then pull gpt-oss 120B Q4, Qwen3 4B, and Llama 4 Maverick Q4.
>
> Port the repo across. Re-run `scripts/bench.py` on all three at 1K/8K/32K/128K context
> and produce a comparison table against the 3080 Ti baseline in `logs/`.
>
> Flag anything that got *slower* — that's a config problem, not a hardware result.

**Done when:** you have the before/after table and nothing regressed.

---

## B1 — TensorRT-LLM and NVFP4

> Move primary inference to TensorRT-LLM with NVFP4 quantization and speculative decoding
> where supported. Keep Ollama as fallback, selectable in config.
>
> Re-run the benchmark. NVIDIA's optimized stack has delivered large gains over the
> baseline config — verify we're getting them.

**Done when:** measurably faster than B0 on the same models.

---

## B2 — Multi-model residency

> Implement the memory budget from PLAN.md. All of these resident simultaneously: gpt-oss
> 120B (primary), Qwen3 4B (fast), VLM, faster-whisper, TTS, embeddings.
>
> Add a memory monitor that logs total unified memory usage and warns at 85%. Benchmark
> the real combination under load before committing — triple-loaded configs are known to
> spike past 130GB during context expansion.
>
> Then remove every model-swap path in the codebase. Nothing unloads during normal
> operation. Re-run the latency report — this should be the biggest single improvement in
> the project.

**Done when:** no swapping, and per-stage latency drops measurably from the 3080 Ti
numbers.

---

## B3 — Model registry and mode switching

> Implement the four-tier registry from PLAN.md: `fast`, `primary`, `heavy`, `alt`.
>
> Two operating configs: **conversational** (primary + full voice/vision stack) and
> **heavy** (Maverick solo, voice stack unloaded per the config `unloads` list).
>
> Switch by explicit voice command or hotkey only — **never auto-failover**. Session
> scoped, auto-reverts on timeout. Active model shown unmistakably on the character.
>
> `alt` runs with a read-only tool set, enforced in the dispatcher, not the prompt.

**Done when:** you can switch to heavy mode for a hard problem and it reverts on its own.

---

## B4 — Nightly self-improvement LoRA

> Build `scripts/nightly_tune.py` using Unsloth. Runs on a schedule against the day's
> accumulated data.
>
> Two adapters, trained separately: (1) persona — trained on reactive lines that landed,
> using the laughter-detection signal; (2) CAD — trained on `cad/dataset.jsonl`, verified
> pairs only.
>
> Hard requirement: **never train on unverified output.** Every CAD pair must have executed
> clean. Keep an eval set and refuse to promote an adapter that scores worse than the
> current one.

**Done when:** an adapter promotes automatically after beating the incumbent on evals.

---

## B5 — Long context

> Now that KV cache headroom exists, raise the primary context window to 128K and adjust
> the rolling-summary threshold accordingly.
>
> Measure the KV cache memory cost at full context and confirm it fits alongside the
> resident stack. Re-run the latency report — check that time-to-first-token hasn't
> degraded at depth.

**Done when:** long sessions run without summarizing, and TTFT holds at depth.

---

## B6 — CAD heavy mode

> Route CAD generation to the `heavy` tier with the fine-tuned adapter loaded. Raise the
> verification loop cap from 5 to 15 iterations — the reasoning is better, so more
> iterations now pay off.
>
> Add batch mode: queue several parts, run overnight, review in the morning.

**Done when:** a part that failed on the 3080 Ti succeeds here.
