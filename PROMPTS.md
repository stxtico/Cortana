# CORTANA — Step prompts

One step per session (or per clear checkpoint). Copy the prompt, verify the "done when,"
commit, update `CLAUDE.md`, move on.

**Track A** runs on the RTX 3080 Ti today. It is the whole build.
**Track B** needs the Spark. It is upgrades to a system that already works.

Don't skip ahead. Don't bundle two steps into one prompt.

## Progress (as of 2026-08-06)

**Done:** A0, A1, A2, A3, A4, A5a (persona), A6, A7 (memory), A8 (agent loop + tools,
web_search deferred), A9 (write tools + confirmation gate + abort hotkey + shell,
fully verified end-to-end), A10 (`ask_user`, verified end-to-end - see below). A5b
(latency) is partially done and deliberately paused.
**Next:** A11 — Proactive daemon.

Two known limitations discovered during A5a, recorded so they aren't re-chased:

- **Dry wit doesn't fire.** 45 samples across nine conditions — full persona, stripped
  persona, positive examples in the shape rules, exact scenario matching, and `gemma4:12b`
  instead of `e4b`. Zero firings in every condition. Prompt structure and model size are
  both ruled out; this is a capability limit at this model scale. Don't re-litigate it
  without a genuinely new approach (few-shot of the exact transformation, sampling several
  candidates and picking, or a larger model on the Spark).
- **She pads answers with unrequested advice.** "You should check for warpage," "I
  recommend checking the feed lines." Same underlying behavior as the length creep and the
  editorializing closers — fixed twice under different names, regressed both times on new
  scenarios. Negative constraints don't hold well at this model scale. See A5a's last step.

A6/A7 landed hand-rolled, not on Letta — PLAN.md's Phase 2 section has the full
reasoning (a real shared-venv dependency conflict with openWakeWord's onnxruntime pin,
and a deeper shape mismatch: Letta's MemGPT-style agent-managed memory adds per-turn LLM
round trips this project's latency budget doesn't have room for). See CLAUDE.md's A6/A7
entry for the implementation.

**A8's original done-when ("what's the weather in Miami" triggers a search and she
answers from it) is not met — deliberate infrastructure deferral, not a broken build.**
No Docker on this machine, so neither web_search backend is actually reachable:
Tavily needs an API key (chose not to depend on an external service/key at all,
picked SearXNG instead for zero external calls), and self-hosting SearXNG needs
Docker (or bare WSL2 install, more setup than wanted right now). `tools/web_search.py`
is fully built, both backends (`tools/_search_tavily.py`, `tools/_search_searxng.py`)
implemented and unit-tested — it's just not offered to the model until one actually
responds (`services/brain/agent.py` checks `web_search.is_available()` live, every
call, no restart needed once a backend exists). Revised done-when for A8, agreed as
arguably the better test anyway (multi-step, real whitelisted directory, no external
dependency so a failure means the loop is broken rather than the network): "read the
config file and tell me what the wake threshold is" — verified, see CLAUDE.md's A8
entry for what it took to get reliable (not the first thing tried).

**A9: `write_file`, `shell`, `calendar_read`, `email_read` built.** Confirmation gate
and the no-credentials rule are both real code in `services/brain/agent_safety.py`,
not persona instructions - A5a already measured negative persona constraints holding
at roughly two-thirds reliability, not a safety bar. Confirmation is keyboard-only for
now: `agent.py` runs standalone, with no wiring to the mic/STT path yet, so there's no
way for a spoken "yes" to reach the dispatcher - stated plainly, not pretended
otherwise. Global abort hotkey (`ctrl+shift+x` by default) verified functionally (task
cancellation logic confirmed live), though a real physical keypress wasn't tested in
this environment - worth a real check when someone's at the keyboard.

Two live bugs surfaced and fixed during this step, both now standing rules or
practices, not just one-off patches:
- **CLAUDE.md rule #10**: an `is_available()` check must never be capable of starting
  or changing anything, since it runs on every single turn - `tools/_outlook.py`'s
  first version used a COM call that actually launches Outlook if it isn't already
  running, caught by it doing exactly that during testing (a real process, hung 30+
  seconds, no visible window, relaunched once after being killed). Fixed to a
  read-only running-process check first, then `GetActiveObject` (never `Dispatch`) -
  Outlook must already be running for calendar_read/email_read to activate at all,
  permanently dormant otherwise, which is the correct failure mode.
- **`shell`'s `is_available()` had its own bug, caught after the WSL setup step was
  done and it still wouldn't flip true.** WSL creates `/mnt/c` as an empty stub
  directory unconditionally, regardless of the automount setting - the original check
  (`ls /mnt`, anything listed = still mounted) could never return true even with
  isolation genuinely working. Confirmed isolation was fine the whole time by checking
  for actual content (`ls /mnt/c/Windows` failed, `mount` showed no `drvfs` entries) -
  fixed the check to test for emptiness at `/mnt/c` specifically instead. Full pipeline
  then verified for real: a whitelisted `echo` through the real dispatcher (confirmed,
  executed inside `CortanaShell`, correct output), a decline (`whoami`, not executed),
  and `rm -rf /` - confirmed by the user at the prompt, still blocked by the whitelist
  inside `execute()`, proving the confirmation gate and the whitelist are independent
  layers. See CLAUDE.md's A9 entry for the full diagnostic path (it wasn't obvious).
  That "test for emptiness at /mnt/c" fix was itself still an inference, not a direct
  check - later rewritten to check `mount`'s own output for a `drvfs` entry instead
  (the real ground truth) after the user found `/mnt/c` was an empty leftover
  directory the emptiness check had been silently relying on. Re-verified clean
  against the confirmed-isolated distro, including a combined hostile-argument test
  (`cat` on a path with both a `/mnt/c` traversal attempt and shell metacharacters) -
  both defenses held at once. See CLAUDE.md's A9 entry for the details.

**A10: `ask_user` built and verified live.** Genuinely spoken through the real TTS
engine, not printed-and-called-done - the answer side is honestly keyboard-only for
now (same gap as A9's confirmation gate: `agent.py` has no mic/STT wiring yet). The
one-question-per-turn cap is dispatcher-enforced (`agent.py`'s `run_agent()`), not
trusted to persona.md alone - proven under adversarial testing, not just asserted: a
deliberately double-ambiguous request made the model try to ask again 6 more times
after the first real question, and every one was correctly blocked. A real bug
surfaced along the way, in a different module than the one being tested:
`tools/shell.py`'s WSL subprocess call didn't set `stdin=DEVNULL`, so it silently
consumed piped input meant for `ask_user`'s later `input()` call, surfacing as an
unrelated-looking `EOFError`. Fixed there and defensively in `tools/_outlook.py`'s
subprocess call too. See CLAUDE.md's A10 entry for the full trace.

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
> `async def stream(messages, tools=None) -> AsyncIterator[str]`; supports OpenAI-format
> tool calling; logs time-to-first-token and total duration as JSON lines to
> `logs/brain.jsonl` on every call.
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

## A3 — Voice: streaming TTS with the cloned voice

The target is a specific cloned voice, so **Coqui XTTS v2 is the production engine.**
Kokoro is the development baseline — build the plumbing against something fast and
predictable, then switch. Have the source audio for cloning ready before starting.

### Step 1 — Engine-agnostic TTS layer

> Build `services/voice/tts.py`, **engine-agnostic from the start**: engine selected in
> `cortana.toml`, with Kokoro and Coqui XTTS v2 both supported behind one interface.
> Implement Kokoro first — fast and predictable, which is what I want while getting the
> plumbing right. Don't hardcode anything Kokoro-specific into the calling path.
>
> The critical requirement: `async def speak_stream(token_iterator)` buffers incoming
> tokens until a sentence boundary, then immediately synthesizes and plays that sentence
> while more tokens are still arriving. Do not wait for the full text.
>
> Also add `sanitize()` that strips markdown, code fences, list markers, and URLs before
> synthesis — spoken output must be plain prose.
>
> Log time-to-first-audio-chunk tagged with the active engine, so the two are directly
> comparable later. Reuse one persistent model instance per process (rule 7).

### Step 2 — Prep voice references

> I have source dialogue for XTTS voice cloning at [path]. Build
> `scripts/prep_voice_refs.py`:
>
> 1. Run silero-vad over the source to find speech segments; drop anything under 6s or
>    over 25s.
> 2. For each candidate compute RMS level, an estimated noise floor from leading/trailing
>    silence, and spectral flatness — clips with music or effects underneath score badly
>    on these.
> 3. Transcribe each with our existing Whisper to confirm single-speaker clean speech.
>    Flag any where confidence is low; that usually means something else is in the mix.
> 4. Rank by cleanliness and export the top 15 as individual WAVs to `voice_refs/`, with a
>    manifest listing metrics and transcript for each.
>
> Also flag separately any segment containing the wake word or the assistant's name in
> ordinary dialogue — those are useful hard negatives for the wake-word model.
>
> Don't pick a final reference. I'll listen to the candidates myself.

**Choosing among them.** The metrics are good at *rejecting* bad clips and bad at
predicting which good clip clones well:

- **Audition 3-5, not 1.** Same test sentence through XTTS from each reference, pick by
  ear. Different clips produce noticeably different clones.
- **Clean beats representative.** A dry, boring line outperforms a dramatic one.
- **Match the everyday register.** She'll mostly say short factual things. Clone from
  calm, level delivery — emotional source audio bleeds that affect into "your calendar is
  clear tomorrow," which is worse than a neutral voice.

### Step 3 — Switch to XTTS and measure

> Switch `[voice].engine` in `cortana.toml` to XTTS v2 with the chosen reference from
> `voice_refs/`. Kokoro stays available as a fallback engine, selectable in config.
>
> **Cache the speaker latents** — XTTS recomputes the speaker embedding from the reference
> on every call unless told otherwise. Compute once at startup, reuse for the process
> lifetime. Same principle as rule 7, and the single biggest XTTS optimization.
>
> Then re-measure: time-to-first-audio-chunk for XTTS vs. the Kokoro baseline, at short
> (one sentence), medium, and long outputs. Report both against the 200ms budget.
>
> If XTTS blows the budget badly, don't tune it yet — give me the numbers first. Options
> we'd weigh: shortening the first sentence so the first chunk is cheap, or accepting a
> higher budget for the first chunk specifically and keeping the rest streaming.

**Done when:** she speaks in the target voice, audio starts before generation finishes,
and you have XTTS vs. Kokoro latency numbers side by side.

### Step 4 — Pauses: make being wrong cheap (do before A4)

Endpoint detection starts at 322ms — `min_silence_duration_ms` (300) + `speech_pad_ms`
(30). Measured against real speech, natural hesitation gaps ran **582–1822ms**, so no
value in the 300–500ms range makes any difference at all: every real pause blows past all
of them.

That kills the obvious approach. A threshold high enough to never clip a thinking pause
(~2s) would tax *every* interaction with two seconds of silence before she responds. A
threshold low enough to feel responsive will interrupt you.

**So stop trying to get the threshold right. Make guessing wrong cheap instead.**

#### The backchannel

When she thinks you've stopped but the thought looks unfinished, she prompts instead of
answering: *"and…?"*, *"go on"*, *"you were saying?"*

That converts the failure mode from *interrupts and answers the wrong thing* — which
derails you — into *gently prompts you to continue*, which costs you nothing. You keep
talking, she stops. It also handles the case a threshold never could: when you genuinely
finished but trailed off ambiguously.

With this in place the threshold stops being critical. Set it around 600-800ms and let the
backchannel absorb the errors.

> Build backchannel prompting in `services/ears/`:
>
> 1. On endpoint, run the partial transcript through the fast model: does this look like a
>    finished thought or an abandoned one? Cheap call, ~100ms.
> 2. If abandoned, play a short backchannel instead of passing the utterance to the brain.
>    **She writes her own** — a background job during idle time generates fresh
>    backchannel lines in her voice and pre-renders the TTS into a pool. Pre-rendering is
>    non-negotiable here: a prompt arriving 800ms after I trail off has already missed its
>    moment. Lines expire on use, never repeat within a session, pool regenerates when it
>    runs low. Same architecture as the camera-cover reactions.
>    Keep them genuinely short — "and?", "go on", "you were saying?" — length is the point,
>    not variety for its own sake.
> 3. If I resume within a few seconds, append to the previous utterance rather than
>    starting fresh, so the whole thought arrives intact.
> 4. Rate-limit hard, and escalate patience: after one unanswered prompt, wait
>    substantially longer before the next. Twice is attentive; five times is nagging.
>
> Raise `min_silence_duration_ms` to 600ms as the new default, since the backchannel now
> covers the error case.

#### What not to build

**Don't have her predict what you were going to say and say it.** Completing someone's
sentence is the most irritating thing a conversational system can do — when it's wrong it
doesn't just fail to help, it derails the thought you were having. A 12B model working
from a partial sentence will be wrong often.

The useful half without the annoying half: let her predict *internally* to judge whether
the thought looks complete, and never speak the prediction. That's already step 1 above —
prediction as a signal, not as speech. Keep it that way.

**Done when:** you trail off mid-sentence, she says "and?" instead of answering, and
picking the sentence back up produces one complete utterance rather than two fragments.

#### Later refinement: per-context voice references

XTTS caches speaker latents, and swapping them costs ~30ms — so different situations can
use different reference clips from the same voice. Backchannels want a quieter, softer
reference than her main responses; the prep run produced 387 candidate segments and only
one is in use.

That's the real lever on how she sounds. Two others, in order of value:

- **Better reference clips.** If a specific word or phrase consistently sounds wrong, the
  fix is a reference containing that phoneme pattern cleanly — not tuning.
- **Pronunciation overrides.** For names and technical terms she mangles ("Aiden", CAD
  jargon, model names), a pronunciation dictionary. Not a model problem.

**What not to build: a voice that "improves itself" from its own output.** XTTS is
inference against a fixed model — it isn't learning as it runs. Making it self-improve
would mean fine-tuning on its own generations, which are by definition lower quality than
its training data. The voice drifts *away* from the reference, artifacts compound, and the
degradation is slow enough that you won't notice until it's badly wrong. There's also no
feedback signal here: unlike the reactive lines (where laughter tells you what landed),
nothing tells you whether a rendering sounded good.

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

### What this actually found

Four real bugs, all from the first live pass — worth expecting rather than being surprised
by:

1. **The barge-in hook over-fired.** Cancelling on *every* wake trigger includes the wake
   that starts the next turn, silently killing responses mid-generation. Gate it on
   playback having actually started, not on a response task merely existing.
2. **A failing response task died silently.** If nothing `await`s it, asyncio logs the
   exception at GC time and you see nothing. `task.add_done_callback` logging every
   outcome is what made the rest of A4 diagnosable at all — add it before you need it.
3. **`stream.abort()` doesn't preempt a blocking `write()`.** A hung audio thread survived
   cancellation and crashed the process at interpreter shutdown. Write in ~100ms
   sub-blocks so cancellation has real checkpoints.
4. **Concurrent XTTS calls corrupt shared model state.** A pool refill overlapping a real
   response produced a negative position index and a fatal CUDA assert — `cached_prefix_emb`
   is shared mutable state. Serialize every call touching the model behind one lock.

The last one presented as random, intermittent CUDA crashes. It was diagnosed by patching
`nn.Embedding` to bounds-check on the CPU side, turning a fatal assert into a catchable
Python exception. Worth remembering as a technique.

---

## A5a — Persona

The character brief. Do this *after* A4, once you've heard her respond to something real —
the generic version is what makes it obvious what to write.

> Build `config/persona.md` from `config/persona_reference.md` (character brief — source
> material to rewrite from, do NOT load it into the system prompt) plus `PLAN.md`'s
> "Persona & reactive moments" section.
>
> Formula: **useful information + confident observation + subtle sarcasm or emotion.**
> Information first, personality second.
>
> Structure it with: how she handles being wrong; how she disagrees with me; what she's dry
> about vs. what she takes seriously; verbal rhythm; and 8-10 sample lines in contexts that
> actually occur — CAD, printing, calendar, files, the business. Not game contexts.
>
> Weight toward the registers that actually fire — calm information is most of it. Keep the
> vulnerable register rare.
>
> Then run real questions through the live persona so I can hear it.

### Four fixes that came out of testing it

Each of these was a real failure found by running live questions, not a hypothetical:

1. **Pushback fabricated specifics.** She invented "requires a minimum of 1.2mm for the
   infill density" — confident, precise, made up. The brief taught the pushback *shape*,
   and with no tools to check anything she filled it with invented numbers. Fix: grounds
   means something she can verify *right now*. Without a tool, she states a suspicion and
   asks the question that would settle it. A confident wrong number is worse than no
   pushback.
2. **Response-shape rules got buried** as the brief grew. Move them to the very top,
   before any character material.
3. **She disputed a correct correction.** Told "PLA prints at 220, not 210," she countered
   with 215 — the disagreement trait overriding the being-wrong trait, plus another
   invented number. Fix: write the precedence explicitly. Disagreement is for what I'm
   about to *do*, not for corrections of her own claims.
4. **Editorializing closers.** "Hopefully that keeps things moving smoothly." Fix: she ends
   on the last piece of actual information. No summarizing closer, no reassurance.

### The ongoing part

Persona is never finished. When a response makes you wince, paste it into `persona.md` with
what she should have said instead. Those before/after pairs beat anything written in
advance.

**Done when:** she sounds like a specific character rather than a stock assistant, and the
response-shape rules hold across several different question types.

---

## A5b — Latency tuning

> Here's my latency report: [paste output].
>
> Work through the stages that miss budget, biggest gap first. For each: diagnose, propose
> the fix, implement, re-measure. Don't add any features during this step.

### What this actually found

**Rewrite the budget before optimizing against it.** The original targets were guesses made
before anything was measured, and the ~1.15s total turned out to be physically unreachable.
Two stages are structural floors, not tunable:

- **VAD endpoint, 610ms** — `min_silence_duration_ms=600`, chosen deliberately after
  measuring real hesitation gaps at 582-1822ms. Backchannel prompting covers the cost. That
  door is closed.
- **Ollama's `load_duration`, ~300ms** — measured on *every* call, warm or cold, both
  endpoints. Not a cold-start signal despite the name. A fixed per-call floor on this
  setup.

**Watch for double-counting.** `ttfc_ms` is measured from `speak_stream()`'s entry — the
same moment the LLM call starts — so it already *contains* the TTFT wait. Summing both
inflates every total. Use engine-synthesis-only in the derived number.

**Real numbers:** achievable floor ~1.9s, currently ~2.3s. The whole remaining gap is one
stage (LLM TTFT residual), which resisted a full diagnosis session — persona prompt,
history growth, endpoint choice, and GPU contention were all ruled out.

**Paused deliberately.** ~400ms from a stubborn stage is a poor trade against building
features. Revisit when it annoys you.

**Done when:** you've rewritten the budget against real component costs and closed the gaps
worth closing. Not when every stage hits a number invented before you knew what things
cost.

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
