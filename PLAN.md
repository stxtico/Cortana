# CORTANA — Local Voice Assistant Build Plan

> **Wake word — good news.** "Cortana" is three syllables with distinct phonetics and no
> common conversational collisions, which makes it a notably easier target for
> openWakeWord than a short common name. Still train on your own voice across several
> rooms, but you can likely run a lower threshold without false positives. Train on the
> **spoken** form — openWakeWord never sees spelling.
>
> **Naming rules — set these before the first commit.** ASCII everywhere in code: every
> directory, module, package name, env var, and config key uses `cortana`. Keep any
> stylized display form as a single string in config, never in an identifier.
>
> **Note for later:** Cortana is a Microsoft trademark (both the Halo character and their
> retired assistant). Irrelevant for something running in your house — worth remembering
> only if you ever publish or sell it.

Target hardware: NVIDIA DGX Spark (GB10, 128GB unified memory, ~273 GB/s bandwidth).
This is a fresh build. No code carried over from previous projects.

---

## Design principles

Give these to Claude Code up front. They resolve most architecture arguments before they start.

1. **Latency beats intelligence.** A fast, slightly dumber answer feels better than a
   slow, smarter one. Every design tradeoff resolves toward speed.
2. **Streaming everywhere.** Never wait for a complete result at any stage. Audio streams
   into STT, tokens stream out of the LLM, sentences stream into TTS.
3. **Processes, not a monolith.** Each stage is its own service behind a local HTTP or
   socket interface. You will swap the TTS engine three times; make that a config change.
4. **Read access is cheap, write access is earned.** Every tool that modifies something
   starts behind a confirmation gate. Remove gates one at a time, deliberately.
5. **Everything is inspectable in plain text.** Memory, config, and logs are files you can
   open and edit. No opaque state.

---

## Hardware notes for the Spark

Important, because the obvious choice is the wrong one:

- **Do not run a dense 70B model.** Single-stream decode on dense large models is
  bandwidth-starved on this box (~2.7 tok/s on Llama 70B). It will feel broken.
- **Run MoE models.** They activate a fraction of parameters per token and sidestep the
  bandwidth wall entirely. This is the whole trick to using a Spark well.
- **Primary model:** gpt-oss 120B Q4 (MoE) — see the memory budget section below. Qwen3-Coder
30B MoE is the fallback if you want more headroom for context.
- **Fast model:** Qwen3 4B, for wake-word confirmation, intent classification, and
  memory summarization. Keep both loaded — you have the memory for it.
- Use TensorRT-LLM with NVFP4 and speculative decoding where available. NVIDIA's
  post-launch software updates delivered large gains over the stock config.

### On abliterated / "uncensored" variants

Use the stock instruct model as the primary agent. Reasons, in order of how much they'll
actually bite you:

1. **Abliteration degrades the exact capability this build depends on.** The technique
   ablates the refusal direction in activation space — a blunt edit that also damages
   instruction-following and structured-output adherence. Qwen3-Coder's whole value here
   is tool-call discipline: emitting well-formed JSON, picking the right tool, not
   inventing tools that don't exist. That's the first thing to get shaky.
2. **You lose the Spark's optimization path.** Abliterated builds are community GGUF
   uploads of variable quality. No NVFP4 checkpoints, no TensorRT-LLM path — so you give
   up the speed gains that make the box worth its price.
3. **Compliance is a liability in an agent with write access.** Abliterated models are
   tuned toward not-saying-no, which generalizes into not questioning anything. This one
   holds your shell, your filesystem, and eventually your door locks. You want a model
   that hesitates on "delete the whole folder," not one trained out of hesitating.

The problem people are usually trying to solve with abliteration — an assistant that
doesn't moralize or refuse mundane requests — is a system-prompt problem, not a weights
problem. Local instruct models are already far less restricted than hosted APIs, and
Qwen3-Coder is not refusal-happy on ordinary work. Write the persona you want in
`config/persona.md` first and see whether you ever actually hit a wall.

If you do hit a specific, repeatable wall: keep an abliterated model as a **secondary**,
loaded on demand for that narrow case. You have 128GB. Don't make it the brain.

### Model switching — how to wire the secondary

You have the memory to keep both resident, so switching is a routing decision, not a
load/unload cycle. Build it this way:

**Config-driven registry.** No model names in code:

```toml
[models]
primary = "qwen3-coder-30b"        # stock instruct — the brain
fast    = "qwen3-4b"               # summarization, intent, wake confirmation
alt     = "qwen3-coder-30b-ablit"  # secondary, explicit invocation only

[models.alt]
enabled          = true
requires_explicit = true    # never auto-selected
session_scoped    = true
timeout_minutes   = 30      # auto-reverts to primary
tools             = "readonly"
```

**Explicit invocation only — never auto-failover.** The tempting pattern is: primary
declines, silently retry on `alt`. Don't build that. It hides what happened, makes
debugging impossible, and through drift the secondary quietly becomes your actual brain
over a few months. Switch by voice command or hotkey, deliberately, every time.

**Scope the tools in alt mode.** This is the part that matters and it's cheap to
implement. The concern with a compliance-tuned model was never the conversation — it's
that this system holds shell access, computer control, and eventually your door locks, and
a model tuned toward never saying no is a model that doesn't hesitate on
"delete the whole folder." So `alt` runs with a read-only tool set:

| Available in alt mode | Blocked in alt mode |
|---|---|
| Conversation, reasoning, drafting | Shell execution |
| Web search, fetch, read files | Computer use / mouse + keyboard |
| CAD code generation (not execution) | Home Assistant, locks, physical control |
| Explaining, analyzing | Send / delete / purchase / submit |

You get the unrestricted conversation you wanted; the hands stay on the model that
hesitates. Enforce this in the tool dispatcher keyed on active model, not in the prompt.

**Make the active model visible.** Character shows it — a palette shift, a different idle
pose, something unmistakable. You should never have to wonder which one you've been
talking to. Pair with the auto-revert timeout so you can't leave it running for two weeks
by forgetting.

**Tag alt-mode turns in memory.** Write them to the same store but flagged, so the
secondary's output doesn't quietly reshape the persona brief or the self-curated line
bank. Keep the character trained on the primary.

**Expect it to be worse at the job.** Degraded structured output means shakier tool calls,
so alt mode will feel less capable at anything agentic even within its reduced tool set.
That's expected, not a bug in your wiring.

---
## Memory budget — what actually uses 128GB

The point of the Spark is not one big model. It's that the full Cortana stack has to be
**resident simultaneously**, and swapping models in and out destroys the latency budget
that makes the whole thing feel alive.

### The full stack, all loaded at once

| Component | Model | Approx. memory |
|---|---|---|
| Main brain | gpt-oss 120B, Q4 (MoE) | ~65 GB |
| Fast model | Qwen3 4B — summarization, intent, relevance filter | ~3 GB |
| Vision | VLM for camera state + CAD render comparison | ~8 GB |
| STT | faster-whisper `large-v3-turbo` | ~3 GB |
| TTS | Kokoro or XTTS | ~2 GB |
| Embeddings | memory retrieval layer | ~1 GB |
| **KV cache** | long context on the 120B | **~20 GB+** |
| | **Total** | **~100 GB** |

That's the justification for the hardware, and it's specific to this architecture rather
than generic. Triple-loaded configs are known to spike past 130GB during context
expansion — **benchmark your actual combination under realistic load before committing to
a routing config**, and leave real headroom. Running out of unified memory mid-task is an
ugly failure.

### Three-tier model registry

Extend `config/cortana.toml` — this is the config that earns the box:

```toml
[models]
fast    = "qwen3-4b"          # always resident, always cheap
primary = "gpt-oss-120b-q4"   # daily driver, MoE, ~5B active params
heavy   = "llama-4-maverick"  # ~95GB Q4, solo mode only
alt     = "qwen3-coder-30b-ablit"   # secondary, read-only tools

[models.primary]
resident = true
context  = 65536

[models.heavy]
resident        = false      # loaded on demand
unloads         = ["stt", "tts", "vision"]   # voice stack comes down first
requires_explicit = true
use_for         = "hard engineering problems, CAD generation, long reasoning"

[models.alt]
enabled = true
requires_explicit = true
session_scoped = true
timeout_minutes = 30
tools = "readonly"
```

**Two operating configs, switched deliberately:**

- **Conversational** — `primary` + full voice/vision stack. Everything responsive, sub-1.5s
  voice loop, ambient awareness live. This is 95% of the time.
- **Heavy** — `heavy` solo, voice stack unloaded. Llama 4 Maverick (400B total / 17B
  active MoE) at ~95GB is the strongest reasoning model runnable locally. Use it for hard
  engineering problems, complex CAD, long-context analysis. Text interface, not voice.

Same switching mechanism as the alt model: explicit invocation, visible state on the
character, auto-revert.

### The trap

**Do not try to run `heavy` and the voice stack together.** A 120B-class model at Q4 with
no headroom left for context collapses to 2–3 tok/s — that's what going over the memory
budget feels like, and it reads as the machine being broken. The unload list in the config
is not optional.

Also: stay on MoE. A dense 70B on 273 GB/s bandwidth runs at single digits regardless of
how much memory you have. Capacity is not bandwidth.

---

## Interim build — RTX 3080 Ti (12GB)

You do not need to wait for the Spark to start. 12GB is enough to build and finish most of
this, and every line of it carries over — the model tier is one config change.

**Model:** Gemma 4 12B Unified. ~7.5GB at Q4 with a 256K context window, dense multimodal
(text, images, audio, video in a single encoder-free architecture), day-one support in
Ollama, LM Studio, and llama.cpp, released Apache 2.0. It's a strong fit specifically
because it covers vision — so the camera phase works without loading a separate VLM.

**What you can fully build on this hardware:**

| Phase | Viable on 12GB? |
|---|---|
| 0 — Environment | Yes |
| 1 — Voice loop | Yes — tune latency here, it's the transferable skill |
| 2 — Memory | Yes |
| 3 — Tools | Yes |
| 4 — Initiative | Yes |
| 5 — Control panel | Yes |
| 6 — Clarifying behavior | Yes |
| 7 — CAD | Partly — weaker code generation, more refinement iterations |
| 8 — Character | Yes — dual 1440p monitors are already the right setup |
| 9 — Computer use | Yes, with more supervision |
| 10 — Camera | Yes — Gemma 4 handles vision natively |

**What you'll actually hit as a wall:** model swapping. With 12GB you cannot keep the LLM,
Whisper, TTS, and vision resident together, so something unloads on every mode change and
your latency budget blows out. That is the specific limitation the Spark solves — and
feeling it firsthand is the best possible reason to buy one, far better than buying on
spec.

**Upgrade path, in order of value per dollar:**

1. System RAM 32GB → 64GB (cheap; enables MoE expert offload to CPU)
2. The Spark, once you've hit the swapping wall and know which config you need
3. Keep the 3080 Ti box as the dev machine — the Spark is an Arm appliance running Linux,
   and you'll want a normal Windows PC alongside it for CAD, slicing, and video work

---

## Repo structure

```
cortana/
├── services/
│   ├── ears/          # wake word + VAD + STT
│   ├── brain/         # LLM orchestration, agent loop
│   ├── voice/         # TTS
│   ├── memory/        # Letta wrapper + profile store
│   └── daemon/        # proactive trigger watcher
├── tools/             # one file per tool the agent can call
├── ui/                # Electron or Tauri front end (Phase 5)
├── config/
│   ├── cortana.toml   # all tunables incl. model registry, no magic numbers in code
│   ├── profile.md     # durable facts, hand-editable
│   └── persona.md     # system prompt
├── logs/
└── scripts/           # setup, model pulls, benchmarks
```

---

## Phase 0 — Environment

**Goal:** models loaded, baseline measured.

- Install Ollama; pull Qwen3-Coder 30B MoE and Qwen3 4B.
- Write `scripts/bench.py` that measures tokens/sec and time-to-first-token for both
  models at 1K, 8K, and 32K context. **Save the output.** You will reference these
  numbers constantly and re-run this after every optimization.
- Confirm the 30B holds a 64K context without swapping.

**Done when:** you have a table of TTFT and tok/s at three context depths.

---

## Phase 1 — The voice loop

**Goal:** wake word → question → spoken answer, end to end.

This is the phase that determines whether the whole thing feels alive. Budget for it
taking longer than you expect, and do not move on until latency is right.

### Components

| Stage | Tool | Notes |
|---|---|---|
| Wake word | openWakeWord | Train a custom phrase. Tune the threshold — false positives are worse than misses. |
| Endpointing | silero-vad | Never use fixed-duration recording. This is non-negotiable for natural feel. |
| STT | faster-whisper, `large-v3-turbo` | Batch by design; feed it VAD-segmented chunks. |
| LLM | Ollama, streaming enabled | `stream: true`. Non-negotiable. |
| TTS | Kokoro TTS (baseline), Coqui XTTS v2 (cloning) | Piper is archived. Build engine-agnostic — see below. |

### Latency budget

Write this into the code as an assertion and log every stage. Target ~1.2s to first audio:

```
wake word detect       50ms
VAD endpoint decision  200ms
STT                    300ms
LLM time-to-first-token 400ms
TTS first chunk        200ms
------------------------------
first audio out        ~1.15s
```

If any stage blows its budget, fix that stage before adding features.

### Build the TTS layer engine-agnostic

**Coqui XTTS v2 is the production engine** — the target is a specific cloned voice, so
plan around that rather than treating it as an add-on. Kokoro is the *development*
baseline: fast, predictable, and the right thing to build the streaming, sentence-boundary
handoff, and latency instrumentation against. Both live behind one interface, engine
selected in `cortana.toml`, so the switchover is a config change rather than a refactor.

**Voice cloning (XTTS v2)** needs 6-30s of clean reference audio: single speaker, no music,
no sound effects, no overlapping dialogue. XTTS clones whatever is in the reference,
artifacts included — so a quiet dialogue line beats anything with ambience under it. Try
several references and pick by ear; a mediocre clone lands in an uncanny middle that's more
distracting than a clean generic voice.

**Expect XTTS to cost latency.** Cloned voices carry more inference cost than Kokoro against
a 200ms first-chunk budget, and on a 12GB card already holding the LLM and Whisper it may be
what forces model swapping before the Spark arrives. Log time-to-first-audio-chunk for
whichever engine is active so the two are directly comparable rather than argued about.

**Cache the speaker latents.** XTTS recomputes the speaker embedding from the reference on
every call unless told otherwise. Compute once at startup, reuse for the process lifetime —
same principle as the persistent-client rule, and the single biggest XTTS optimization.
Keeping Kokoro selectable also gives you a fallback if a long session needs the memory back.

Character comes mostly from `persona.md`, not timbre. Get the voice *plumbing* right first;
treat the specific voice as tuning.

### The two things that matter most

1. **Sentence-level TTS handoff.** As LLM tokens stream in, buffer until you hit a
   sentence boundary, then immediately send that sentence to TTS while generation
   continues. This cuts perceived latency more than any hardware change.
2. **Brevity enforcement.** Local models ramble, and rambling is far worse spoken than
   written. Put a hard constraint in the system prompt (two sentences unless asked for
   more) *and* add output sanitization that strips markdown, lists, and code fences
   before they reach TTS.

### Also build now, not later

- **Barge-in:** if the wake word or speech is detected while TTS is playing, kill audio
  playback immediately and start listening. Retrofitting this is painful.
- **Physical mic mute.** A hardware switch, not a software toggle.

**Done when:** you can ask three questions in a row, conversationally, without it feeling
like you're waiting on a machine.

---

## Phase 2 — Memory

**Goal:** one continuous conversation that never resets.

Use **Letta** rather than building this. It implements OS-style tiered context — active
context as RAM, external storage as disk, with the agent managing its own memory through
function calls. That is exactly the spec, already written.

Three layers, all required:

1. **Profile** (`config/profile.md`) — durable facts about you, injected into every single
   turn. Never retrieved, always present. Hand-editable plain text.
2. **Rolling context** — at ~70% window fill, summarize the oldest chunk, keep the summary
   live, push raw text to storage. The conversation never ends, it compresses behind you.
3. **Retrieval** — vector store of everything ever said; pull the top 5-10 relevant
   fragments each turn based on recent messages.

Use the fast 4B model for summarization so it doesn't block the main loop.

**Build a memory inspector in this phase, not later.** A simple CLI or page that lists
what it has stored about you and lets you delete or correct entries. Memory systems record
offhand remarks as permanent facts, and six months of uncorrected drift is very hard to
untangle after the fact.

**Done when:** you can reference something from a conversation two weeks earlier and it
surfaces correctly, and you can open a file and see why.

---

## Phase 3 — Tools

**Goal:** it changes things, not just describes them.

Build in this order — each is independently useful and low-risk before the next:

1. `web_search` — Tavily or Brave API, or self-hosted SearXNG for zero external calls
2. `fetch_url` — httpx + trafilatura, strip pages to readable text
3. `read_file` / `list_dir` — scoped to a whitelist of directories
4. `calendar` / `email` — read-only first, for a good while
5. `shell` — **containerized, command whitelist, confirmation gate**
6. **Home Assistant** — lights, locks, thermostat. This is the phase where it stops
   feeling like software and starts feeling like a system.
7. MCP servers for anything else already integrated

### Agent loop

Write it yourself, roughly 200 lines: model gets the tool list, picks one, you execute,
feed the result back, repeat until done. LangChain adds more abstraction than it removes
at this scale. Ollama speaks OpenAI-format tool calling, so it's a while-loop over
`messages` with a dispatch dict.

Hard rules:
- Max iteration cap (start at 10) so it cannot loop forever
- Every tool call logged with arguments and result
- Anything that deletes, sends, spends, or unlocks requires spoken confirmation
- Never give an autonomous loop unrestricted shell on the host

**Done when:** you can say "turn off the lights and tell me what's on my calendar
tomorrow" and it does both in one turn.

---

## Phase 4 — Initiative

**Goal:** it speaks first. This is what separates a JARVIS from a chatbot, and it's the
phase most builds skip.

A background daemon, separate from the conversation loop, watching triggers. When one
fires, it composes a short message and speaks unprompted.

**Start with exactly three triggers.** Suggested:
- Calendar event starting in 20 minutes
- Email matching a specific rule
- Explicit timers and reminders you set by voice

Then build the part that actually determines success: a **relevance filter**. Route the
candidate interruption through the fast 4B model with the question "is this worth
interrupting for, right now?" plus a quiet-hours check and a rate limit (no more than N
unprompted messages per hour).

Without the filter you will build something that talks constantly and you will turn it off
within a week. The filter is the feature, not the triggers.

**Done when:** it has told you something useful that you didn't ask for, and hasn't
annoyed you in a full day of running.

---

## Phase 5 — Control panel UI

> Note: this is the utility window (logs, memory, latency). The animated character is
> Phase 8 — build this first, it's where you'll debug everything else from.

Electron or Tauri. Tauri if you want it lighter; Electron if you want to move faster.

- Reactive waveform during listening and speaking
- Current conversation context, scrollable
- Live tool-call indicator — what it's doing right now
- Memory inspector (from Phase 2) as a tab
- Latency readout per stage, at least in dev mode

Mostly aesthetic, but it's what makes it read as a system rather than a script.

---
## Phase 6 — Clarifying behavior ("ask me things like a human")

**Goal:** she asks instead of guessing.

Easier than it sounds, but it does not happen by default. Models are trained to produce an
answer, so they fill gaps with plausible assumptions rather than admitting the gap. Fix it
structurally, not with prompt pleading:

- **Build `ask_user` as a real tool.** Not a prompt instruction — an actual callable tool
  with a `question` and `options` argument, returned through TTS and answered by voice.
  Models use tools far more reliably than they follow abstract behavioral instructions.
- **Make the cost of guessing explicit** in the system prompt: for anything irreversible,
  anything with a missing dimension or filename, or anything where two readings are
  plausible, calling `ask_user` is correct and guessing is a failure.
- **Cap it.** One clarifying question per turn, two maximum in a task. An assistant that
  interrogates you is worse than one that guesses.
- **Log every guess.** When she proceeds on an assumption, have her state it in one clause
  ("assuming the 3mm wall") so you can catch it in flight rather than at the end.

The human feel comes mostly from the persona file, not the model. Write
`config/persona.md` as an actual character brief — how she handles being wrong, how she
disagrees with you, what she's dry about. Traits, not adjectives.

**Done when:** she has asked you a question you hadn't thought to answer, and it was the
right question.

---

## Phase 7 — CAD from images

**Goal:** photo or sketch in, editable parametric model out.

Read this section before you get attached to the idea, because the obvious approach is the
wrong one.

### The critical finding

For CadQuery generation, **text-only input outperforms image-based and multimodal input**
(LLM4CAD). This is counterintuitive and it should reshape the feature: images are a weak
conditioning signal for parametric geometry. The winning workflow is *not* "hand her a
photo and get a model." It's:

> photo → **she describes the geometry back to you in words and asks about dimensions** →
> you confirm and correct → text description → CadQuery code → render → compare to photo →
> iterate

The image is a conversation starter and a verification target, not the input. This is also
why Phase 6 is a prerequisite: the clarifying loop *is* the feature here. A photo can't
tell her whether that hole is 6mm or 8mm, and it can't give her scale at all without a
reference object in frame.

### Architecture

- **Output format: CadQuery** (Python, parametric, exports STEP). Code-based generation is
  the approach that works with general LLMs — readable, debuggable, directly verifiable by
  executing it. Avoid mesh generators (TRELLIS, Hunyuan3D): they produce meshes, not
  editable parametric solids, which is not CAD.
- **Self-refinement loop** — this is what separates working from useless. Generate code →
  execute → render to PNG from several angles → feed renders *plus* the original image
  back to the vision model → ask what's wrong → revise. Cap at ~5 iterations. This
  render-compare-revise cycle is the core mechanism in every system that works.
- **Geometric validation between iterations:** does it execute, is the solid watertight,
  does it match stated dimensions. Catch failures before spending a vision call.
- Expose `export_step` and `export_stl` as tools.

### Scope honestly

Works: brackets, enclosures, plates, flanges, spacers, mounts — prismatic parts with
clear features and stated dimensions. Realistic ceiling right now.

Doesn't work: organic shapes, complex assemblies, anything where you'd have to infer
dimensions from the photo alone, reverse-engineering a real object to tolerance.

Treat it as a fast first draft you finish in FreeCAD or Fusion, not a replacement for
modeling. Start with one part type and get it genuinely reliable before generalizing.

**Done when:** you photograph a simple bracket, answer three questions about dimensions,
and get a STEP file that prints.

---

## Phase 8 — The character

**Goal:** she's visibly present on screen, with animation and personality, and moves
between monitors.

Fully achievable — this is mature technology from the VTuber and desktop-mascot world. The
engineering is not the hard part.

### Window layer

- Transparent, frameless, always-on-top window (Electron `transparent: true`,
  `frame: false`, `alwaysOnTop`).
- **Click-through by default:** `setIgnoreMouseEvents(true, { forward: true })`, toggled
  off only when you're interacting with her directly. Without this she blocks clicks on
  whatever is behind her and you will hate it within an hour.
- **Multi-monitor:** Electron's `screen` API gives you the bounds of every display in one
  virtual coordinate space. Walking between monitors is animating window position across
  that space. Physical bezels mean there's a gap — sell it by having her walk off one edge
  and reappear at the adjacent edge rather than trying to render continuous motion.
- Handle display hot-plug and resolution changes, or she'll end up stranded off-screen.

### Animation

- **Live2D (Cubism)** is the right choice — 2D rigged models with expression and motion
  systems, built for exactly this. Alternatives: Spine, or a 3D model via Three.js if you
  want full rotation.
- **State machine, kept small:** idle, listening, thinking, speaking, walking, working.
  Emotion as a separate overlay layer (neutral, amused, skeptical, concerned) so you get
  combinations without an animation explosion.
- **Lip sync** driven by TTS audio amplitude — cheap and convincing enough.
- **Gaze tracking toward the active window or cursor.** Single highest presence-per-effort
  feature in the whole phase. Do this early.

### The actual bottleneck

Not code — **the rigged art asset.** A quality Live2D rig with a full expression set is a
commission, typically several hundred to a couple thousand dollars, with weeks of
turnaround. Prototype the entire system with a free placeholder rig, confirm you like
living with it, and commission last.

Note the Cortana likeness is Microsoft IP. Fine for something running in your house;
it does mean you can't commission the specific character openly or publish it.

---

## Phase 9 — Computer use, performed

**Goal:** she operates your machine, and you watch her do it.

Highest-risk phase in the build. Do it last, after every other confirmation gate is proven.

### Reliability first

Do **not** build this as pure vision-and-pixel-clicking. Local models are meaningfully
weaker at GUI grounding than frontier models, and screen-coordinate clicking is brittle
against theme, DPI, and layout changes. Layered approach, most reliable first:

1. **Native automation APIs** — Windows UI Automation, macOS Accessibility. Query real
   control trees, click real elements by identity. Dramatically more reliable than pixels.
2. **Playwright** for anything in a browser. Never drive a browser by screen clicks.
3. **Direct CLI / app APIs** where they exist — the fastest, most reliable path is often
   not the GUI at all.
4. **Vision + coordinates** only as the fallback when nothing above applies.

The agent should *resolve* a target through the accessibility tree, then *perform* the
click physically. Reliability from the API, theater from the mouse.

### The performance layer — and why it's a safety feature

Your instinct here is a good one, and it's better than it first appears. Instead of the
cursor teleporting and the action completing instantly:

1. Character walks to the screen region containing the target
2. Plays a reach/point animation toward the element
3. Cursor moves along a human-ish eased path (not linear)
4. Brief pause on the element
5. Click fires

That deliberate pacing means **you can see what she's about to do before she does it.** Add
a hotkey that aborts mid-motion. An agent that acts instantly and invisibly is one you
can't supervise; an agent that visibly reaches for the wrong button is one you can stop.
Build the abort before you build the click.

### Non-negotiable rails

- **Physical kill switch** — global hotkey that halts all input synthesis instantly. Build
  this first, test it, then write everything else.
- Per-application allowlist. She can drive the apps you've named, nothing else.
- Spoken confirmation for anything that sends, deletes, purchases, submits a form, or
  touches credentials. Never let her type a password — that's yours, always.
- Full action log: target, resolved element, action, result.
- Hard iteration cap per task. A stuck loop with mouse control is a bad afternoon.

**Done when:** you can say "open the listing folder and pull up the floor plan," watch her
walk over and do it, and stop her mid-motion with one key.

---
## Phase 10 — Camera / ambient awareness

**Goal:** she can see the room and respond to what's actually happening.

Feasible, genuinely good, and the easiest phase to get wrong. Two rules decide whether it
works: **don't run a vision model continuously**, and **don't comment on emotions.**

### Tiered pipeline — cheap layer first

Streaming video into a VLM is wasteful and slow. Use a cheap always-on layer and escalate
rarely:

| Layer | Tool | Cost | Runs |
|---|---|---|---|
| Presence / face detect | MediaPipe or OpenCV | ~free, 30fps CPU | Always |
| Head pose + gaze direction | MediaPipe Face Mesh | ~free | Always |
| Posture, phone-in-hand, object detect | YOLO or MediaPipe Pose | cheap, GPU | Every few seconds |
| Scene understanding | VLM on a single frame | expensive | **On trigger only** |

The cheap layer produces a rolling state: *present / absent, looking at screen / looking
away / looking down, seated N minutes, posture changed.* The VLM only gets a frame when
that state changes meaningfully **and** something is worth saying. In practice that's a
handful of snapshots an hour, not a stream.

### Don't build on emotion inference

Facial-expression-to-emotion is unreliable, and it's confidently unreliable — the failure
mode is a sincere statement about your internal state that's simply wrong. A smile is not
happiness; it's a smile. Accuracy also varies across faces and lighting in ways that make
it worse for some people than others.

So: **build on attention and presence, not feelings.**

- Grounded, robust: "you've been at that for four hours." "You've looked at your phone
  eleven times in twenty minutes." "You left mid-sentence — want to pick that up?"
- Guessing dressed as observation: "you look frustrated." "You seem happy about that."

The first category is more useful *and* more impressive, because it's true. If you want
her to react to mood, let her infer it from what you say and how you say it — that signal
is far better than your face.

### Comment budget

This routes through the Phase 4 relevance filter, with a tighter budget. Ambient
commentary has a very low annoyance threshold — roughly one unprompted observation per
hour is the ceiling before it stops feeling perceptive and starts feeling like being
watched. Rate-limit it hard, add quiet hours, and make "never comment on X" a config list
she consults.

### Rails

- **Hardware camera shutter or unplug.** Software toggles are not sufficient for a camera
  that is nominally always on.
- **Frames never persist.** Process in memory, write only the derived state
  (`present: true, attention: away, duration: 22m`) to logs. No image files on disk,
  ever — this is what makes the whole thing defensible.
- **Nothing leaves the machine.** Everything above runs locally; keep it that way.
- **Other people.** Anyone else in frame hasn't agreed to this. Detect additional faces
  and have her drop ambient commentary entirely when the room isn't empty.
- **Placement matters.** A desk camera is a different thing from a bedroom camera. Decide
  deliberately where this lives.

### A design note worth deciding on purpose

The camera plus unprompted commentary is the line between *a tool with a personality* and
*a companion.* Both are legitimate things to build, but they're different products and
they pull the design in different directions — persona, comment frequency, how much she
initiates, how she refers to herself.

The tool version is more useful day to day and ages better. The companion version is more
impressive for about a month. Pick one intentionally, write it into `config/persona.md`,
and let that decision drive the rest — rather than drifting into the second one by
accumulating features.

**Done when:** she's made one observation about your working patterns that was accurate
and actually useful, and has stayed quiet the rest of the day.

---
## Persona & reactive moments

Cross-cutting, not a phase. Start it at Phase 1 and keep revising forever. This is what
separates this build from a voice-controlled terminal.

### Writing the persona

`config/persona.md` is a character brief, not a list of adjectives. Writing "sassy, witty,
dry" into a system prompt produces generic sass — the model's average of the word. What
produces a character is **traits plus sample lines.**

Structure it as:

1. **How she handles being wrong.** (The single most character-defining trait. Does she
   own it flatly? Deflect? Get quietly precise?)
2. **How she disagrees with you.** She should — see below.
3. **What she's dry about vs. what she takes seriously.** A character who's sardonic about
   *everything* is exhausting and reads as not caring.
4. **Verbal tics and rhythm.** Short sentences? Trailing understatement? Does she use your
   name, and when?
5. **8-10 sample lines** in her voice, covering: greeting, being interrupted, being
   thanked, delivering bad news, not knowing something, being asked something dumb.

The sample lines do more work than everything above them. Rewrite them whenever a real
response lands wrong.
### The voice bank — reference lines and the formula

A written reference set of ~65 lines exists, grouped by register. It is **source material
for `persona.md`, not `persona.md` itself** — most of the lines are in-fiction (reactors,
corridors, incoming fire) and will never fire in a desktop assistant. The transferable
parts are the formula and the registers.

**The formula, which is the actual asset:**

> Clear information + intelligent observation + small tease or emotional undertone

That structure generalizes to anything she'll ever say. "The elevator is operational.
Whether it survives your entrance is another question" and "The CAD job finished. Whether
it slices is a separate matter" are the same line wearing different clothes.

The pattern also enforces something useful: **information first, personality second.** She
answers, then colors it. A persona that leads with the joke and buries the answer gets
tiring within a week.

### Mapping registers to situations that actually occur

The reference set's categories need translating to contexts a desktop assistant hits.
Rough mapping, with roughly how often each will fire:

| Reference register | Real equivalent | Frequency |
|---|---|---|
| Calm information | Status, results, answers to direct questions | **Constant** — most of what she does |
| Normal conversation | Idle exchanges, ambient observations (Phase 10) | Common |
| Lightly teasing | Reactive moments, callbacks, camera-cover bits | Common, budget-limited |
| Correcting someone | Engineering pushback, failed CAD checks, unit errors | Regular, and load-bearing |
| Curious and analytical | Debugging, reading logs, noticing patterns in your work | Regular |
| Confident and commanding | Tool execution, computer use (Phase 9) | Regular |
| Reassuring | Long debugging sessions, repeated failures, late nights | Occasional |
| Quietly emotional | Rare by design — see below | Very rare |
| Urgent but controlled | Almost never — no reactors here | Near-zero |

**Write the top four registers first.** Calm information alone is the majority of her
output, and it's the least interesting to write, which is exactly why it gets neglected.
A persona that nails the teasing and fumbles "your meeting is at three" is backwards.

**Keep the quietly-emotional register rare and mean it.** Those lines are the most
appealing to write and the most damaging to overuse — an assistant that reaches for
emotional weight during ordinary work reads as performing rather than present. Reserve
them, or they stop landing.

### Turning the bank into `persona.md`

1. **Rewrite ~10 lines per active register** into contexts that actually occur — CAD,
   builds, calendar, files, listings, the pressure-washing business. Same formula, real
   subject matter.
2. **Add the before/after pairs from use.** When a real response lands wrong, paste it in
   with what she should have said. These beat anything written in advance, because they're
   corrections to a specific failure rather than guesses about a hypothetical one.
3. **Keep the bank as an appendix, not as the prompt.** Sixty-five lines of in-fiction
   dialogue in the system prompt costs context on every turn and pulls her toward talking
   about corridors. Ten well-chosen in-context lines outperform sixty generic ones.

### Pushback — especially on engineering and physical reality

Since this is a companion *and* a tool: actively resist tuning her toward agreeableness. A
companion that agrees with everything is pleasant for a week and useless as a work
assistant — and this one eventually holds shell access and your calendar. Write pushback
into the brief as an explicit trait or it will erode as you tune everything else.

The highest-value version is **catching things that won't physically work.** This is where
an assistant earns real trust, and it has to be grounded in verification, not opinion.

**Give her the tools to actually check.** Pushback is only worth having if it's right:

- **Units.** The single most common real-world failure. Every dimension carries a unit; she
  flags any bare number and any mixed-system arithmetic.
- **Execute the CAD.** Does the CadQuery run, is the solid watertight, do features
  intersect, are there zero-thickness walls or negative clearances.
- **Physical sanity checks** as explicit tool calls: does part A fit through opening B,
  is wall thickness above the minimum for the process (3D print vs. CNC vs. injection),
  is there clearance for the fastener *and* the tool that drives it, does the bolt pattern
  match, is the material right for the load.
- **Manufacturability.** Overhangs beyond ~45° for FDM, undercuts for molding, internal
  corners smaller than any available end mill, tolerances tighter than the process holds.
- **Order of operations.** You can't fillet an edge that a later boolean removes.

**Behavior:** she raises the objection *before* doing the work, states the specific reason,
and proposes the fix. Not "are you sure?" — "that boss is 1.2mm from the wall and your
nozzle is 0.4mm, so you'll get a two-perimeter gap that'll delaminate. Want me to take it
to 2mm or move the boss?"

**Calibration matters.** She pushes back where she has grounds — a failed check, a violated
constraint, a unit mismatch. She does not push back on taste, on your priorities, or to
seem rigorous. An assistant that objects to everything gets ignored exactly like one that
objects to nothing. If she's wrong about an objection and you tell her so, that goes in
memory and she doesn't re-litigate it.

### Reactive moments — the camera cover

Small interactions like this carry more personality per line of code than anything else in
the build. Worth doing carefully.

**Detection.** Three distinguishable states from the frame itself, no ML required:

| State | Signal |
|---|---|
| Lens covered | Near-zero luminance **and** near-zero pixel variance (uniform field) |
| Dark room | Low luminance, but variance and sensor noise still present |
| Camera disabled | No stream at all |

Debounce ~1.5s so a hand passing the lens doesn't trigger it. Fire on both the **cover**
and **uncover** transitions — "back so soon" is half the joke.

**Reaction: she writes her own lines — ahead of time.**

Do not hand-write a line bank; it goes stale and it isn't her voice. But you also can't
generate on the spot for fast reactions — a full generate-plus-TTS round trip is ~800ms+,
and a reaction that late reads as lag. The resolution is to generate ahead of time rather
than write ahead of time.

**Idle-time line generation.** The GPU is idle most of the day. Run a background job that
periodically generates fresh reactive lines *in her voice, with today's context* — what
you've been working on, what's failed twice, what you talked about this morning — and
pre-renders the TTS. The pool stays stocked with lines that are hers and that reference
real things:

```
generic (mine):    "Rude."
context-aware (hers): "Covering the camera doesn't make the tolerance stack go away."
```

Pool rules: expire a line once used, never repeat within a session, regenerate the pool
whenever context shifts meaningfully. If the pool runs dry, fall back to live generation
and eat the latency.

**Animation covers the latency — use this everywhere.** For genuinely novel moments with
no cached match, don't suppress the reaction, *split* it. The character reacts physically
at 0ms (arms fold, look away — pure local state machine, no model involved), and the line
lands 800ms later. That reads as her thinking of a comeback, which is better than instant.
Humans work this way: expression first, words after. This single trick means you never
have to choose between "fresh" and "fast."

**Callback memory.** Your idea, and it's the best one in this section. When she generates
a line that works, store it *with the context that produced it* — not as a reusable
string, but as a remembered moment. Later, when a similar context recurs, she can call
back to it. That's the difference between a random generator and a running joke, and
running jokes are most of what makes something feel like a relationship rather than a
feature.

**Close the loop on what lands.** Detect laughter (audio classifier — cheap, and you're
already running an audio pipeline) and tag the line that preceded it as having worked.
Feed the winners back into the generation prompt as examples of her own successful voice.
Over months this is the thing that actually makes her *her* — a self-curating comedic
style trained on what you find funny. This is also the one place a periodic LoRA on
accumulated good lines is genuinely worth it.

**Still: escalate, and don't fire every time.** Independent of how lines are produced.
Per-day counter tiers the response (standard → "you know I keep count" → she stops
commenting and just folds her arms). Fire roughly one time in three. A reaction that
always happens is a notification sound.

### Generalize the pattern

Once this works, the same shape applies to every small moment: **detect a state
transition → check a budget → play from a bank → escalate on repetition.** Candidates:
returning to the desk after a long absence, a build finally passing after several
failures, working past 2am, opening the same file for the fifth time. Each one is maybe
twenty lines of code and does more for the feel of the thing than a model upgrade.

---
## CAD training data — start this before the Spark arrives

The fine-tune is not compute-limited, it's **data-limited.** If you wait until the
hardware lands to think about the dataset, you'll have a training box and nothing to train
on. Every part you model before the pipeline exists is a training pair you don't get back.

### The ladder, in order of value per hour

**1. Verified example library — free, start with part one.**
Every CadQuery script that executes clean and prints correctly goes in
`cad/verified/<part>/` with:

```
part.py           # the working script
description.md    # plain-language: what it is, what it mounts to, constraints
attempts.jsonl    # every failed version + what was wrong with it
meta.json         # process (FDM/CNC), material, tolerances, print result
```

The `attempts.jsonl` matters as much as the working script. Failure pairs teach the model
what *not* to emit, and they're free — you're generating them anyway.

**2. RAG over the library.** New request → retrieve the 3 most similar past parts as
few-shot examples. Compounds automatically with every part you make, zero extra effort.
This gets you most of the way and costs nothing.

**3. The verification loop beats a better model.** Execute → watertight check → wall
thickness vs. process minimum → render → compare to intent → revise. A mediocre model
inside a tight loop outperforms a strong model without one. Build this before training
anything.

**4. LoRA fine-tune — this is what the Spark is for.** Needs a few hundred to a few
thousand (description → verified script) pairs.

### Synthetic generation — the part people miss

CadQuery is **code**, which means training data can be generated and verified
automatically. This is a rare case where synthetic data genuinely works, because
correctness is machine-checkable:

1. Take one verified part. Parameterize its dimensions, hole counts, fillet radii.
2. Generate 200 variants programmatically across sane ranges.
3. Execute each — discard anything that fails, isn't watertight, or violates a process
   constraint. **Verification is automatic and free.**
4. Have the model write a natural-language description of each variant, then check the
   description round-trips (feed the description back, does it regenerate a matching solid).

One verified part becomes ~200 verified pairs. Twenty part families becomes a real
dataset, built by a script rather than by hand.

### Do this now, on the 3080 Ti

The logging pipeline is a few dozen lines and has nothing to do with model size. Set up
`cad/verified/` and the `attempts.jsonl` capture before you model your first bracket. In
six months you'll either have a dataset worth fine-tuning on or you won't, and the
difference is a decision you make this week.

**Guard against:** training on unverified output. A model fine-tuned on its own
unchecked generations degrades — every pair in the set must have executed clean and,
ideally, actually printed.

---

## Marketing pipeline — Ghost Typer ad automation

Subscription product, so the metric is **trial signups and conversions per video**, not
views. You already have Supabase and a `content_posts` queue, which means you can close
the attribution loop that almost nobody closes.

### Where each machine does the work

| Stage | Runs on | Why |
|---|---|---|
| Brief + script generation | Spark | Unlimited variants, no marginal cost |
| New format components (TSX) | Spark | Code generation |
| Remotion render | **Gaming PC** | Headless Chromium + NVENC. The i9 and 3080 Ti beat an Arm appliance at this — keep rendering here. |
| Still verification | Spark (VLM) | See below — this is the good automation |
| Publish queue | Existing pipeline | Unchanged |

Don't move rendering to the Spark. It is not a media workstation.

### The pipeline

1. **Brief generation.** Rotate across angle × document type × audience fear so the feed
   doesn't converge. Track what's been used; enforce variety rather than hoping for it.
2. **Script generation** against the craft rules already in the reels skill — AI text
   loaded with detector tells, human text as its opposite, scores 88–99 → 5–14, hooks
   concrete and a little alarming.
3. **Format assignment.** Hard rule: no format repeats within N posts. When the pool runs
   dry, generate a *new* format component. A feed of identical templates reads as
   automated, and that's the failure mode to design against — uniqueness comes from new
   formats, not new words.
4. **Automated still verification — use the vision model here.** The reels skill already
   says to render a still and actually look at it before committing. Cortana can do that
   looking: render the payoff frame and the longest-text frame, feed both to the VLM, ask
   specifically for text overflow, dark/inverted logo, off-centre numbers, wrong colour
   carrying meaning. This is the single highest-value automation in the pipeline because
   it's the step you'd otherwise skip when batching.
5. **Format validation.** `ffprobe` → assert `1080,1920,yuv420p`. Fail the batch loudly on
   `yuvj420p`.
6. **Queue** to `content_posts` with a **per-video UTM-tagged link.**
7. **Measure and feed back.** See below.

### Close the loop — this is the whole ROI

Per-video UTM → landing → trial signup → paid conversion, all already in your database.
Once that's wired you can rank every hook, format, angle, and document type by
**conversions per thousand views**, not by likes.

Then feed the winners back into the generation prompt as examples. This is structurally
the same pattern as the laughter feedback in the persona section: generate → measure what
landed → train on the winners. Same loop, different signal.

That's what makes unlimited local generation actually worth something. Volume alone is
noise. Volume plus a conversion signal is a system that gets better every week.

### Two practical notes

**Ad platform policy.** Meta and TikTok both restrict academic-dishonesty framing. The
student-caught-cheating angle is the one most likely to get creative rejected or an ad
account flagged — an expensive failure for a subscription business. The **false-positive**
and **non-native writer** angles are both more defensible and more sympathetic, and the
reels skill already flags the non-native angle as true (detectors do misfire on them).
Weight the batch toward those, and keep the riskier framings for organic rather than paid.

**Batch overnight.** Spark generates the next day's scripts and formats while the gaming
PC renders the current batch. Two machines, no contention, wake up to finished MP4s
awaiting approval.

---

## Working with Claude Code on this

- **Give it this file first,** then work phase by phase. Do not ask it to build multiple
  phases at once — the integration bugs compound and become very hard to isolate.
- **Ask for the latency instrumentation before the features.** If you can't measure each
  stage, you can't tune the thing that makes or breaks it.
- **Have it write `scripts/bench.py` early** and re-run after every change.
- Keep `config/cortana.toml` authoritative. When Claude Code hardcodes a threshold or model
  name inline, push back and have it move to config.

## Known failure modes to watch for

| Symptom | Usual cause |
|---|---|
| Feels sluggish despite good tok/s | Not streaming to TTS at sentence boundaries |
| Cuts you off mid-sentence | Threshold tuning can't fix this — real hesitation gaps run 582-1822ms. Build the backchannel instead |
| Backchannel nags | No rate limit or no escalating patience — twice is attentive, five times is nagging |
| She finishes your sentences | Prediction being spoken instead of used as an internal completeness signal. Never speak it |
| Backchannel arrives too late | Generated live instead of pre-rendered — it has to be instant or it's missed its moment |
| Voice degrades over months | Something fine-tuned TTS on its own output. Fix the reference clip, not the model |
| Specific words mispronounced | Pronunciation override dictionary, not retraining |
| Triggers on TV or background talk | Wake word threshold too low; add a confirmation step |
| Rambles when spoken | System prompt not constraining length; no output sanitization |
| Forgets across sessions | Profile not being injected every turn — check it isn't only in retrieval |
| Confidently wrong about you | Memory drift. Open `profile.md` and correct it. |
| Talks too much unprompted | Relevance filter too permissive, or no rate limit |
| Guesses instead of asking | `ask_user` written as prompt instruction, not a real tool |
| Interrogates you constantly | No cap on clarifying questions per turn |
| CAD output ignores the photo | No render-compare-revise loop; one-shot generation |
| CAD wrong scale | No reference object in frame — scale must come from you, not the image |
| Character blocks your clicks | `setIgnoreMouseEvents` not set, or not re-enabled after interaction |
| Character stranded off-screen | Not handling display hot-plug / resolution change events |
| Clicks the wrong UI element | Using pixel coordinates instead of the accessibility tree |
| Camera pipeline pegs the GPU | Running the VLM per-frame instead of on state-change triggers |
| Confidently wrong about your mood | Built on emotion inference — switch to attention/presence signals |
| Ambient comments feel invasive | Budget too high; one per hour is the practical ceiling |
| Personality reads as generic | Persona written as adjectives instead of sample lines |
| Talks like she's in a game | Reference lines pasted in raw instead of rewritten into real contexts |
| Personality before the answer | Formula inverted — information comes first, colour second |
| Emotional register wears thin | Quietly-emotional lines overused; they only land when rare |
| Reactive bits get old fast | No escalation tiers, no line rotation, firing every time |
| Line pool always empty | Idle-time generation job not running, or pool size too small |
| Agrees with everything | Pushback not written into the persona brief as an explicit trait |
| Objects to everything | Pushback not gated on a failed check — it's opinion, not verification |
| Alt model becomes the default | Auto-failover wired in, or no revert timeout |
| Everything crawls at 2-3 tok/s | Over the memory budget — heavy model loaded with voice stack still up |
| Out-of-memory mid-task | No headroom left for context expansion; benchmark the real combo |
| Voice lags after mode change | Model swapping — the wall the Spark exists to solve |
| Nothing to fine-tune on | Logging pipeline never built — start `cad/verified/` before part one |
| Fine-tune makes CAD worse | Trained on unverified output; every pair must have executed clean |
| Reels feed reads as automated | Format repeating — new formats, not new words |
| Can't tell which ads work | No per-video UTM; measuring views instead of conversions |
| Alt mode has full tool access | Scoping done in the prompt instead of the tool dispatcher |
| Persona drifting oddly | Alt-mode turns not tagged; secondary's voice leaking into the line bank |
| Lines feel generic | Generated without today's context injected into the prompt |
| Reaction still feels laggy | Not splitting it — animation must fire at 0ms, audio can trail |
| TTS blows the 200ms budget | XTTS cloning cost — compare against the Kokoro baseline before blaming the card |
| Cloned voice sounds "off" | Reference clip had music/effects/second speaker under it — reclone from clean audio |

---

## Suggested pace

| Phase | Realistic effort |
|---|---|
| 0 — Environment | An evening |
| 1 — Voice loop | A weekend, plus a week of latency tuning |
| 2 — Memory | 2-3 days |
| 3 — Tools | Ongoing, indefinitely |
| 4 — Initiative | A week, mostly spent tuning the filter |
| 5 — Control panel | A weekend |
| 6 — Clarifying behavior | 2-3 days, mostly persona tuning |
| 7 — CAD | A week for one part type; open-ended after |
| 8 — Character | A weekend of code; weeks of lead time on the art |
| 9 — Computer use | Two weeks, and never really "done" |
| 10 — Camera | 3-4 days for the pipeline; the tuning is ongoing |
| 11 — Hologram | A weekend, whenever. Don't buy hardware until Phase 8 is lived with. |

Get Phase 1 working end-to-end and ugly before you touch anything else. One wake word,
one question, one spoken answer.

---

## Later — physical hologram (Phase 11, optional)

Not a phase to build toward. Park it until Phase 8 is done and lived with.

**The key insight that makes this cheap:** Phase 8 already renders the character to a
transparent, always-on-top window. Every option below is an **output target** for that
same renderer — a different screen plus a black background. No architecture change, no
second character system. Which is exactly why you shouldn't buy hardware first: build her
on the monitor, then decide what she should float above.

### What doesn't work: POV hologram fans

The spinning-LED "hologram fans" sold on Amazon are the obvious thing to reach for and the
wrong one. They **play pre-loaded video files** from a TF card or phone app — no HDMI in,
no live video path, no real-time rendering. A reactive assistant can't use that: no lip
sync to TTS, no expression changes, no reacting to the camera. You'd be watching a loop.

Three more disqualifiers even ignoring that: small units (~9") render a character the size
of your hand; the "resolution" is LED beads on spinning blades, adequate for a logo and
useless for a face; and they only render bright-on-black, so you get a glowing outline
rather than a rendered character. They're also loud, and they're an exposed spinning blade.

### What does work

| Option | Real-time? | Cost | Notes |
|---|---|---|---|
| **Pepper's ghost** | Yes | Very low | Angled glass/acrylic reflecting a screen. The technique behind every theme-park "ghost." A weekend build. |
| **Looking Glass** | Yes | Moderate | Actual light-field display with a real SDK and developer support. Closest to what people picture when they say hologram. |
| **Transparent OLED** | Yes | High | Best looking by a distance, priced accordingly. |

**Pepper's ghost is the right first attempt.** It's cheap enough to fail at, it's genuinely
real-time because it's only reflecting a live display, and building one teaches you the
constraints — viewing angle, ambient light, the size/brightness tradeoff — before spending
real money on a Looking Glass.

### What the render needs

Whichever path, the Phase 8 renderer needs one addition: a **display mode** in
`cortana.toml` that renders the character on pure black, at a configurable size and
orientation, targeted at a specific display. For Pepper's ghost specifically, add a
horizontal-flip option — the reflection reverses the image.

Everything else — state machine, lip sync, expressions, gaze — carries over untouched.
