# CORTANA

Local voice assistant + agent. Companion and work tool. Read `PLAN.md` for full context
and rationale — this file is the operating summary.

## Current state

**Phase:** 1 — Voice loop
**Hardware:** RTX 3080 Ti (12GB VRAM), i9-12900K, 32GB RAM, dual 1440p, Windows 11
**Model:** Gemma 4 12B Unified (Q4, ~7.5GB, multimodal — covers vision too), Ollama tag
`gemma4:12b`

> Update this block every session. It's the first thing to read and the thing most likely
> to be stale.

**Done:**
- A0 — repo skeleton, uv project, `config/cortana.toml`, `scripts/bench.py`. Baseline on
  `gemma4:12b` (thinking off, `keep_alive=30m`): TTFT ~500-650ms, ~58-62 tok/s, flat across
  1K/8K/32K context — still ~1.5x over the 400ms budget, revisit in A5.
  `logs/bench-2026-08-03.json`.
- A1 — `services/brain/client.py`. Async `stream(messages, tools=None, think=False)` over
  `/api/chat`, OpenAI-format tool calling passed through and smoke-tested (model correctly
  emitted a `get_weather` tool_call, yielded as JSON). Per-call TTFT/duration/tokens logged
  to `logs/brain.jsonl`. Client reuses one module-level `httpx.AsyncClient` (`aclose()` to
  shut down) — first version opened a new one per call, costing ~280ms in connection setup
  every turn, which was the whole gap between this and bench.py's numbers (endpoint choice
  and `num_ctx` explicit-vs-default were both noise, isolated separately). Warm TTFT now
  ~360-390ms after the first call in a process, matching bench.py.
- A2 — `services/ears/{wake,vad,stt}.py` + `pipeline.py`. silero-vad (`VADIterator`,
  endpoint-only, real decision latency read off `current_sample`/`temp_end`) +
  faster-whisper `large-v3-turbo` on GPU (needed `nvidia-cublas-cu12`/
  `nvidia-cudnn-cu12` pip packages + their DLL dirs on `PATH` — ctranslate2 doesn't
  find CUDA otherwise on Windows). One shared 512-sample/32ms frame size feeds both
  wake and VAD. `debounce_s` (2.0) in `[audio.wake]` prevents openWakeWord's
  internal embedding window (~1-2s post-detection) from spuriously re-triggering
  right after a genuine detection.
- Wake word: **trained model in production**, not the `hey_jarvis` stand-in.
  Live calibration first proved score alone can't separate real detections from
  false accepts on `hey_jarvis` (a false accept hit 0.987, above most genuine
  hits) — added a second-stage STT verification gate
  (`[audio.wake].verify`/`verify_phrase` in `cortana.toml`) as an interim fix,
  then trained a real `hey_cortana` model (`config/models/wake/hey_cortana.onnx`):
  20,000/3,000 positive samples across 6 Piper voices (US/GB, both genders),
  50,000 steps, full MIT RIR + 3,400 background clips (AudioSet+MUSAN). Full run
  took ~50 minutes on the 3080 Ti via WSL2 (see `services/ears/WAKE_TRAINING.md`
  for the training-pipeline compatibility fixes and process — genuinely useful
  if training a second model, e.g. bare "cortana", later). Live comparison
  (same 3-minute continuous-speech-then-wake-phrase protocol against both
  models): trained model beat the `hey_jarvis`+verification baseline outright —
  0 false accepts survived verification vs. baseline's 2-of-6, 0 genuine
  detections wrongly rejected vs. baseline's 1, and zero raw-score frames
  exceeded 0.9 across 180s (baseline's false accepts regularly hit 0.98-0.99).
  Verification gate stays on for now — one 3-minute session isn't enough data
  to trust dropping it.
- A3 Step 1 — `services/voice/{engine,kokoro_engine,tts}.py`. Engine-agnostic
  interface (`TTSEngine` ABC: `synthesize()`, `sample_rate`, optional `close()`) so
  switching `[voice].engine` kokoro -> xtts (step 3) touches config only.
  `speak_stream()` splits streamed LLM tokens on sentence boundaries and runs
  synthesis/playback concurrently with continued token accumulation via a queue +
  `asyncio.gather` (verified live: sentence 2's tokens fully arrived while sentence
  1 was still mid-playback). `sanitize()` strips markdown/code/links/URLs before
  synthesis. Found `uv add kokoro` had silently installed CPU-only torch
  (`torch.cuda.is_available()` was False, no error) — pinned `torch` to the PyTorch
  cu132 wheel index in `pyproject.toml` to match the driver; confirmed on the
  3080 Ti now. Clean TTFC baseline (`logs/voice.jsonl`, engine warm, single-call,
  real content not filler): short (19 chars) 115ms, medium (67 chars) 146ms, long
  (189 chars) 307ms against the 200ms budget — short/medium clear it, long misses
  by ~100ms. Added `config/persona.md`: first sentence of any response must be
  short, since first-chunk latency is set by the first sentence's length, not the
  whole response.
- A3 Step 2 — `scripts/prep_voice_refs.py`. ffmpeg-decodes source dialogue, segments
  offline with silero-vad's `get_speech_timestamps`, scores SNR/spectral-flatness/
  Whisper-confidence, exports ranked WAV candidates + manifest. Two ranking modes:
  `--rank-by snr` (acoustic quality) and `--rank-by calm` (added on request - low RMS
  variance, moderate level, low pitch variance via a hand-rolled autocorrelation F0
  tracker since librosa/numba can't resolve against numpy>=2.5, slow speech rate;
  z-scored and combined). Also flags wake-phrase segments as hard negatives and
  likely comms/radio-filtered clips (in-band energy fraction - calibrated threshold
  0.75 against real data, an initial guess of 0.97 missed a genuinely filtered clip
  entirely). Picked `voice_refs/calm/voice_ref_14.wav` (calm_14) by ear.
- A3 Step 3 — `services/voice/xtts_engine.py`, switched `[voice].engine` to `xtts`.
  Getting XTTS running surfaced three real dependency conflicts, each fixed at the
  root: numba (hard dep via librosa) caps numpy<2.5, relaxed the project's numpy pin;
  coqui-tts's XTTS code breaks on transformers>=5.1 (idiap/coqui-ai-TTS#558), pinned
  transformers<5.1; torchaudio.load() as of 2.9+ requires TorchCodec, whose DLLs
  don't load here, so XTTS's `load_audio()` is monkeypatched to decode via soundfile
  instead. Latents cached once via `set_reference()`, reused for the process
  lifetime (rule 7). Isolated TTFC (engine warm, single call): short 115ms
  (Kokoro)/532ms (XTTS), medium 146/1333ms, long 307/4724ms - XTTS badly misses the
  200ms budget in isolation. But `speak_stream()` pipelines synthesis of sentence
  N+1 against playback of sentence N (three-stage queue: tokens -> sentences ->
  audio -> speakers - a real bug was caught and fixed here, the original version
  synthesized strictly after playback finished, so no engine could ever have shown
  hidden cost), and under a realistic multi-sentence response only sentence 2 showed
  a stall (275-806ms, since its synthesis has nowhere to start earlier than ~300ms
  in and doesn't fit inside sentence 1's short playback) - sentence 3 onward always
  caught up. Tried adding concurrent synthesis workers to fix that stall: worse, not
  better (single GPU, concurrent XTTS calls contend rather than overlap - gap grew
  to 1386-2013ms). Splitting sentence 2 into two shorter sentences fixed it
  completely (0ms gap, both runs) - same total characters, just an earlier sentence
  boundary. Codified as a `persona.md` rule (prefer several short sentences over one
  long compound one, especially early in a response) instead of a code fix.
- VAD pause test (`scripts/vad_pause_test.py`) - one live capture with deliberate
  mid-sentence hesitations, replayed offline against `min_silence_duration_ms`
  300/400/500ms. All three produced identical results: true gaps (via
  `vad_latency_ms`) ran 582-1822ms, so none of the tested thresholds were even close
  to long enough - a value that never clips a real thinking pause would cost ~2s of
  trailing silence on every turn. Conclusion: stop tuning the threshold, make
  guessing wrong cheap instead (backchannel prompting - in progress, see below).
- **VRAM budget on the 3080 Ti: the crash was a fixable DLL bug, not capacity -
  corrected after further investigation, see below for what's still actually true.**
  Original finding (kept for the record, partially wrong): `gemma4:12b` alone
  resident measured ~9.8GB/12GB; adding Whisper large-v3-turbo then XTTS failed
  deterministically with `CUDNN_STATUS_SUBLIBRARY_VERSION_MISMATCH`, reproduced
  twice, attributed to VRAM pressure. **That attribution was wrong.** Reproduced the
  same crash with 10GB+ free and zero Ollama models loaded - merely *importing*
  `services/ears/stt.py` was enough, before any model was even constructed. Root
  cause: `_add_cuda_dll_dirs()` added the standalone `nvidia-cudnn-cu12` pip
  package's DLL directory to the process search path for ctranslate2's sake, and
  Windows started mixing DLLs from that install with torch's own bundled cuDNN
  (`torch/lib/*cudnn*`) - torch's bundle is missing `cudnn_ext64_9.dll` and
  `cudnn_engines_tensor_ir64_9.dll`, so XTTS's conditioning-latent step (a
  torchaudio resample using cuDNN's newer graph API) pulled those two from the
  *other* install and got a version mismatch against the core DLLs already loaded
  from torch's. **Fix:** stopped adding the standalone cudnn directory - every real
  call site already has torch loaded in-process (`vad.py`'s silero-vad pulls it in,
  as does XTTS), so ctranslate2 finds cuDNN via torch's own registration instead.
  Removed the now-unused `nvidia-cudnn-cu12` pyproject.toml dependency. Verified via
  real `transcribe()` and `synthesize()` calls, not just successful loads.
  **With that fixed, measured the real question: does `gemma4:12b` + Whisper +
  XTTS actually fit?** Yes - 11868MB/12288MB used, only 217MB free, but confirmed
  stable with real inference calls on both Whisper and XTTS at that margin, not just
  a load. 217MB is razor-thin, though - zero headroom for context growth, a second
  model (fast-tier, embeddings later), or any transient spike.
  Separately (this part of the original finding **was** genuinely about VRAM, not
  the DLL bug): loading `qwen2.5:1.5b` (1.2GB, the fast-tier completeness-check
  candidate) alongside `gemma4:12b` pushed Ollama to 11.1-11.7GB/12.3GB and caused a
  real eviction - a follow-up `gemma4:12b` call took 6.1s instead of the ~360-390ms
  warm baseline. Decision from that finding stands: the backchannel completeness
  check is a non-LLM heuristic (`services/ears/completeness.py`), `[models].fast`
  stays unused for now.
  **Measured all four headroom options requested, real numbers, not estimates:**
  - *Smaller primary*: `gemma4:e2b` (5.1B params, but only **1.7GB** resident -
    elastic/MatFormer architecture, on-disk size doesn't reflect inference-time
    footprint) and `gemma4:e4b` (8.0B params, **3.2GB** resident) vs `gemma4:12b`'s
    ~9.8GB. Full stack (+ Whisper + XTTS): e2b leaves 4126MB free, e4b leaves
    2632MB, 12b leaves 217MB. Speed is a bonus, not just a VRAM trade: e2b hit
    154.4 tok/s and e4b 109.0 tok/s vs 12b's 62.6 tok/s on the same prompt, similar
    TTFT (585-671ms) across all three. One spot-check prompt (a two-fact status
    update) showed no obvious quality collapse on either smaller variant, but one
    prompt isn't a real eval - worth a broader check before committing.
  - *XTTS on CPU*: frees ~2-3GB (confirmed ~0MB GPU use), but TTFC goes from
    532/1333/4724ms (GPU, short/medium/long) to **2582/4403/18466ms** on CPU - 3.5-5x
    worse, and even the short case blows well past what the sentence-pipelining fix
    from A3 step 3 can hide. Not viable given current latency goals.
  - *Whisper on CPU or smaller*: CPU (`int8`) took 4666ms to transcribe a 5.1s
    utterance vs 170ms on GPU (float16) - 27x slower, unusable alone. A smaller GPU
    model is the real lever here: `small` used **~800MB** (vs large-v3-turbo's
    ~1.5-2GB) and was actually *faster* on one clean test utterance (126ms vs
    170ms) with a correct transcript - but one easy sentence doesn't validate
    accuracy on harder/noisier audio, unlike the primary-model check this needs a
    broader pass before trusting it.
  - *Deliberate swapping via keep_alive*: confirmed the mechanism works exactly as
    documented - a 5s `keep_alive` auto-evicted `gemma4:e2b` and fully freed its
    VRAM with no code beyond the API parameter. But cold-load cost is real: 8.5s
    (`12b`) / 12.8s (`e4b`) / 10.2s (`e2b`), not obviously correlated with model
    size (disk I/O dominates, not the VRAM copy) - roughly matches the earlier 6.1s
    eviction-reload measurement. This rules out swapping *within* a conversational
    turn (nothing in an ~8-13s range is hideable), but stays plausible for
    infrequent, deliberate *mode* boundaries (conversational <-> heavy, B3
    territory) where a "switching modes" pause is expected UX, not a stall.
  **Not yet decided**: which combination to actually ship. The options aren't
  mutually exclusive - e.g. `gemma4:e4b` (2.6GB headroom) + Whisper `small` (saves
  another ~700-1200MB) would leave real room for a fast-tier model and embeddings
  later without touching CPU fallback at all. That's a call for next session, not
  made unilaterally here.

- Backchannel prompting (`services/ears/{completeness,backchannel_pool,backchannel}.py`,
  wired into `pipeline.py`). Raised `[audio.vad].min_silence_duration_ms` to 600ms per
  the VAD pause test finding above, and cover the clipping it still can't avoid with
  a cheap recovery instead of a bigger threshold: `completeness.py` is a **non-LLM**
  heuristic (last-word dangling/filler check + an optional, off-by-default prosody
  signal - see below) since `[models].fast` was ruled out by the VRAM finding.
  `backchannel_pool.py` pre-generates short lines (brain call, persona.md as voice
  context) and pre-renders them through the shared TTS engine during idle time -
  verified end-to-end, real generations came back on-tone and length ("Go on.",
  "And?", "You were saying?"). `take()` never generates live - an empty pool means no
  backchannel that turn, not a fallback to synthesis-on-demand. `backchannel.py` is
  the state machine: abandoned -> play a pooled line (if not rate-limited) -> listen
  for a resume without requiring the wake word again (new `awaiting_resume` state in
  `pipeline.py`, `vad.reset()` + direct VAD feed, bypassing wake/verify) -> append a
  resumed utterance to the pending fragment and re-check -> on timeout, finalize and
  yield the fragment anyway (silence eventually does mean done) and escalate the
  cooldown (`base_cooldown_s * escalation_factor ** consecutive_unanswered`, capped).
  Unit-verified all four transitions (yield / backchannel / resume-and-complete /
  rate-limited-after-timeout) with a fake pool. Tried the prosody signal (F0 trend
  over the utterance tail - falling reads as complete, flat/rising as continuing)
  against real captured hesitation audio, not synthetic TTS: a hand-rolled
  autocorrelation pitch tracker (`services/ears/pitch.py`, factored out of
  `prep_voice_refs.py` to stop duplicating it) called 14 of 17 real segments
  "flat_or_rising" including clean complete sentences - net negative, so it ships
  off by default (`use_prosody=False`), text-only signals validated correctly on the
  same real data. Completeness prediction never leaves this module as speech - it
  only ever selects yield/backchannel/wait, backchannel lines are pre-written generic
  phrases, never a guess at what was coming.
  **Not yet done: a live end-to-end run** (real wake word, real trailing-off speech,
  real resume) - everything above is verified per-component (completeness against
  real hesitation data, pool generation with real brain+TTS calls, the state machine
  with a fake pool) and the `pipeline.py` wiring was reviewed carefully against those
  same interfaces, but nobody has actually talked to it yet.

- **Primary switched to `gemma4:e4b`, Whisper stays `large-v3-turbo`.** STT accuracy
  is load-bearing for both the wake-verification gate and the completeness check, so
  the ~700-1200MB `small` would have saved wasn't worth the accuracy risk on either -
  `small`'s one clean test in the VRAM investigation didn't validate that. Before
  switching, tested `e4b` against A8's actual demands, not just chat quality: 4 tool
  schemas (`web_search`/`fetch_url`/`read_file`/`list_dir`), 6 single-turn selection
  cases including a no-valid-tool case (must decline, not invent one) and an
  ambiguous case (`list_dir` to search vs. guessing a `read_file` path), plus a real
  2-step chain (search -> feed back a mocked result -> must `fetch_url` the *actual*
  URL from that result, not invent one). `e4b` matched `gemma4:12b` exactly on every
  case, including sharing the same "wrong" answer on one (both called `list_dir`
  instead of `read_file` for "what does config/cortana.toml say" - a shared gemma4
  tendency, not an `e4b` regression) - not a single invented tool or malformed JSON
  from either model. Full comparison test script and reasoning in this session; not
  re-saved to the repo (scratchpad only) since it's a one-off validation, not a
  recurring tool.
  New clean `bench.py` baseline on `e4b` (`logs/bench-2026-08-03.json`, overwrote the
  original `gemma4:12b` day-of file - same-day reruns are expected to replace, the
  numbers are in A0's bullet above for comparison): TTFT 382ms (1K ctx) -> 413ms (8K)
  -> 519ms (32K), vs `12b`'s flat ~360-390ms regardless of depth - `e4b` grows more
  with context. Tok/s 107.6 -> 106.0 -> 95.2, vs `12b`'s ~58-62 flat - a real ~1.7x
  generation-speed win that holds across all three depths tested.

- Dual XTTS references + "still sounds robotic" investigation. `XTTSEngine` now
  supports named references (`use_reference(name)` on top of the existing path-based
  `set_reference()`, ~30-50ms to switch, confirmed) - `[voice.xtts.references]` has
  `calm` (voice_ref_14, normal responses) and `soft` (a new full 44.7s clip, picked
  by ear for softer/more human moments despite being past XTTS's ~30s useful
  reference length). Switching logic (when to use which) is deliberately not built
  yet - waiting on the persona work. Inference params (temperature/repetition_penalty/
  length_penalty/top_k/top_p/speed) are now config-driven (`[voice.xtts]`) and
  per-call-overridable on `synthesize()`, not hardcoded - `scripts/xtts_param_sweep.py`
  generates a temperature x speed comparison set for picking values by ear (not
  picked here - that's a listening call).
  Found a real, reference-dependent XTTS failure mode while testing short text:
  autoregressive decoding occasionally misses the stop token and rambles for several
  extra seconds unrelated to the input. Measured 3/60 short-text calls (1-4 words,
  the entire backchannel category) failing across 4 references, clustered entirely on
  2 of the 4 - `calm` (the active default) and `calmest_b` had zero failures in 15
  trials each, `full`/`soft` and `calmest_a` did not. Tried lowering temperature as a
  fix: made it worse, not better (a 27s runaway at temperature=0.4 on the
  worst-affected reference) - not a sampling-randomness problem in the fixable sense.
  Real fix: `services/ears/backchannel_pool.py`'s `_synthesize_with_retry()` - a
  duration-sanity check against input length, retried a few times since the failure
  is stochastic (same input/reference, doesn't fail every time). Verified: 0/20
  outliers survived the retry on the worst-affected reference.
  Built `services/voice/normalize.py` (numbers/decimals/times/units -> spoken form,
  using `num2words` - already a coqui-tts transitive dependency - for the actual
  digit conversion) - not yet wired into `tts.py`'s synthesis pipeline, generated A/B
  comparison samples instead (`scripts/test_normalization.py`) pending a listening
  verdict. Caught and fixed a real bug in its own time-regex while testing edge
  cases: "4:30pm" (no space before am/pm) produced a broken "four:30pm" because the
  trailing `\b` failed between a digit and a letter - fixed by making the regex
  consume an optional am/pm suffix explicitly.
  Isolated streaming-architecture choppiness from reference character: the same
  multi-sentence text synthesized per-sentence-then-concatenated (what
  `speak_stream()` actually does) ran 14.01s total vs. 11.56s for one whole-text
  XTTS call - ~21% longer from cumulative per-call lead-in/lead-out padding plus the
  inserted inter-sentence gaps alone, before even judging how it sounds. Both
  versions generated (`scripts/test_sentence_boundary_prosody.py`) for a by-ear
  verdict on how much of "robotic" is architecture vs. reference.
  **Nothing above picked a winner** - temperature/speed values, normalization
  on/off, and per-sentence-vs-whole-text are all pending the user actually listening.

- Listening verdicts landed: **whole-text prosody wins** (confirmed the path-
  divergence diagnosis - XTTS conditioning on complete text beats isolated
  sentences), reference/params confirmed as `calm`/XTTS-library-defaults (already
  what `cortana.toml` had), **full normalization wired into `tts.py`'s real
  pipeline** (`sanitize()` -> `normalize()` before every `synthesize()` call, not
  just test scripts).
  Diagnosed a reported "pipeline sounds more British than the test script" gap:
  turned out to be neither sanitize() nor a stray config key (checked - `[voice.
  xtts]` only has `default_reference`, no leftover `speaker_wav`) - it was that
  `speak_stream()`'s sentence-splitter fed XTTS the correction text as two isolated
  calls while the test script that produced the preferred `full.wav` synthesized
  both sentences together in one call. Confirmed by direct inspection
  (`engine.active_reference`, `engine._inference_defaults`, and the exact
  post-sanitize/post-normalize text at each stage all matched between paths -
  only the sentence-boundary grouping differed).
  This is why `speak_stream()` now supports `[voice].strategy` (`per_sentence` /
  `whole_text` / `hybrid` / `hybrid3`) - `_CONSUMERS` in `tts.py` swaps out just the
  token-to-chunk grouping, the audio/playback pipeline downstream doesn't change.
  Whole-text alone kills streaming entirely (no audio until the full response
  generates), so built `hybrid` (sentence 1 alone, everything after as one call)
  as the compromise - measured on a realistic 4-sentence response, real XTTS calls,
  token pace matched to `gemma4:e4b`'s actual ~104 tok/s (not an arbitrary rate):
  TTFA held at 283-329ms (matches the standalone-sentence-1 baseline), but the gap
  between sentence 1's playback ending and the remainder starting was 3.1-3.6s
  across two runs - sentence 1's ~0.7-1.5s playback comes nowhere close to covering
  the ~3.4-4.0s a 199-character whole-text call takes to synthesize. Built the
  pre-authorized fallback, `hybrid3` (sentence 1, then sentences 2-3, then the
  remainder): reduces the first gap to 1.6-2.7s (roughly a third smaller) and the
  *second* transition (chunk 2 -> chunk 3) hit 0ms gap on both runs - synthesis
  caught up completely during chunk 2's long playback. But the first gap is still
  substantial either way - more chunking helps some, it doesn't close the gap,
  because the real constraint is XTTS being slow per-character on multi-sentence
  calls relative to how short sentence 1's own playback is. Strategy choice
  (`per_sentence` default retained, not switched to `hybrid`/`hybrid3` yet) is
  still the user's call - numbers reported, not decided here.

**Next:** Strategy decision (per_sentence/hybrid/hybrid3/whole_text - all four
working and measured, tradeoffs above), then a live end-to-end test of backchannel
prompting (needs a real voice - same constraint as the VAD pause test), then A4.

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

## Latency budget (Phase 1, enforced in code)

| Stage | Target |
|---|---|
| Wake word detect | 50ms |
| VAD endpoint | 200ms |
| STT | 300ms |
| LLM time-to-first-token | 400ms |
| TTS first chunk | 200ms |
| **First audio out** | **~1.15s** |

Log every stage every turn. If one blows budget, fix that stage before adding features.

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
