# CORTANA

Local voice assistant + agent. Companion and work tool. Read `PLAN.md` for full context
and rationale — this file is the operating summary.

## Current state

**Phase:** 5 — Control panel (done: A11 daemon, A12 UI — TypeScript, frameless
holographic chrome, real memory edit/delete with a durable deletion audit log
(`logs/memory.jsonl`); next up is 7 — CAD, A13)
**Hardware:** RTX 3080 Ti (12GB VRAM), i9-12900K, 32GB RAM, dual 1440p, Windows 11
**Model:** Gemma 4 Unified, elastic (Q4, multimodal — covers vision too), Ollama tag
`gemma4:e4b` (switched from `gemma4:12b` — 3.2GB resident vs ~9.8GB, validated against
A8's tool-calling demands first, see Done below)

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

- inference_stream: a 5th `[voice].strategy`, built after hybrid/hybrid3 couldn't
  close the mid-response gap (previous bullet). `Xtts.inference_stream()` conditions
  on the *entire* text in one sequence (confirmed by reading the source: `text =
  [text]` when not splitting) while yielding audio progressively as GPT tokens
  generate - whole-text delivery character and streaming output from the same call,
  which chunking could only ever trade off against each other. Checked DeepSpeed
  first (`use_deepspeed=True`) as the other lever: blocked, not just slow - it needs
  the full CUDA Toolkit (`CUDA_HOME`, `nvcc`) to compile its ops, and this machine
  only has runtime libraries. A multi-GB SDK install is out of scope to do
  unilaterally; noting it as available if VRAM/build tooling changes later.
  `inference_stream()` is a synchronous generator (blocks per chunk while the GPU
  works), bridged into the async pipeline via a thread + `asyncio.Queue` -
  `call_soon_threadsafe`, same pattern `services/ears/pipeline.py` already uses for
  sounddevice's mic callback. Added `TTSEngine.synthesize_stream()` to the interface
  (default raises `NotImplementedError`; `KokoroEngine` inherits that, `XTTSEngine`
  overrides it) - `speak_stream()` checks `type(engine).synthesize_stream is
  TTSEngine.synthesize_stream` and falls back to `per_sentence` rather than ever
  triggering the raise for real.
  Raw characteristics measured before playback integration: time to first chunk
  331-714ms (same ballpark as isolated-sentence-1 TTFA), generation-to-playback
  ratio 0.41-0.49x (audio generates roughly twice as fast as it plays), every chunk
  across both test runs arrived before cumulative playback would have needed it,
  margin growing throughout the response (-0.17s at chunk 0 to -5.7s by chunk 12) -
  in principle zero mid-response gaps, unlike hybrid/hybrid3.
  While testing actual playback, found a separate real problem, universal across
  *all* strategies, not just this one: `play_audio()`'s `sd.play()`+`sd.wait()` per
  chunk costs ~60-80ms of stream setup/teardown overhead on top of the audio's own
  duration - real dead air at every chunk boundary that the existing `gap_ms` metric
  couldn't see (it only measures whether the next chunk was *ready* in time, not
  whether the hardware stream stayed continuous). Fixed by switching `_play_all()`
  to one persistent `sd.OutputStream` per response (`.write()` per chunk) instead of
  a new stream per chunk - measured overhead directly: ~67-79ms/chunk before, ~10-22ms
  after. `play_audio()` itself is unchanged, still used for one-off single-clip
  playback (backchannel lines don't need a persistent stream).
  Barge-in implication of the OutputStream switch, fixed alongside it:
  `asyncio.to_thread`-wrapped cancellation only stops *awaiting* the blocking call -
  the underlying thread (and the audio hardware) keeps running unless something
  actually halts the stream. `play_audio()` now calls `sd.stop()` and `_play_all()`
  now calls `stream.abort()` on `CancelledError`, re-raising after.
  **Both verified end-to-end this session** (see below), not just written.

- OutputStream + barge-in verification, and the two findings that blocked the
  strategy comparison. `_play_all()`'s persistent `sd.OutputStream` verified with a
  real `speak_stream(strategy="inference_stream")` run: no clicks/dropouts, 13 real
  chunks, per-chunk write overhead averaged **-13.3ms** (oscillating -20.6 to
  +11.5ms, no positive-spike/underrun signature) - better than the ~10-22ms
  standalone estimate, not just consistent with it. Barge-in verified on all four
  criteria (start a stream, cancel 3s in, confirm stop/no-orphan/no-leak/still-usable):
  `CancelledError` propagated cleanly, zero orphaned threads after a 1s grace period,
  no leaked exception, a fresh `synthesize()` call afterward worked normally.
  Two real findings surfaced along the way, both required resolving before the
  strategy comparison could mean anything: (1) `inference_stream` (via
  `_consume_whole_text`) measured **999ms-1064ms TTFC**, not the fast-first-audio
  win it was supposed to be - because it waits for the *entire* token stream to
  finish before firing the one `inference_stream()` call, same blocking wait as
  `whole_text`. (2) Barge-in testing surfaced XTTS's 250-char `char_limits["en"]`
  warning in the logs; checked what it actually does - source-read confirms
  `check_input_length()` only logs, the real hard stop is a 402-GPT-token `assert`.
  Measured directly with real audio + transcription (not duration ratios alone,
  which looked inconclusive at 91-113% of baseline - see rule 6): text at 357 chars
  synthesized clean every time, 457 chars **truncated 4/4 runs** (always dropping
  the last word). Also confirmed via source (`gpt.py`) that XTTS has no
  incremental/append-mid-generation text feeding mechanism - `store_prefix_emb` sets
  a fixed prefix embedding once from the initial input, so "start on sentence 1 and
  append more as it arrives" isn't possible; the only real lever was *when* the one
  call fires and *how much* text it's given.
- `buffered_stream`: sixth `[voice].strategy`, the fix for both findings above at
  once. Fires the first `inference_stream()` call once sentence 2 completes (or
  ~300 chars, whichever first) instead of waiting for the full response - meaningfully
  more conditioning context than `hybrid`'s sentence-1-alone, without
  `inference_stream`'s full-wait cost. Remainder fires as a second call. Both calls
  routed through a new `_split_into_capped_chunks()` helper at a 350-char hard cap
  (`_MAX_CHUNK_CHARS`) - a safety margin under the 457-char truncation boundary just
  confirmed. `speak_stream()`'s dispatch generalized from a single
  `strategy == "inference_stream"` check to a `_STREAM_CONSUMERS` dict (both
  `inference_stream` and `buffered_stream` need `_stream_synthesize()`, not
  `_synthesize_all()` - `_stream_synthesize()` already looped over multiple
  sequential chunk_queue items correctly, no change needed there).
  Extended the same 350-char cap to the three non-streaming strategies that lacked
  it: `whole_text` (the entire response, unbounded before this), `hybrid`'s
  remainder call, and `hybrid3`'s chunk-2 and remainder calls - all were silently at
  risk of the same truncation on a long enough response, not just `buffered_stream`.
  Verified directly: a 702-char synthetic response now splits into capped
  sub-350-char pieces on all four (`whole_text`/`hybrid`/`hybrid3`/`buffered_stream`),
  none exceeding the cap.
  **Six-way comparison** run on the same realistic 207-char multi-sentence response,
  real word-paced tokens at ~80wps (matching `e4b`'s real generation speed - the
  first run used a mistaken 4wps human-speech pace and produced nonsense numbers,
  caught before trusting them and rerun). TTFA / max mid-response gap / total time:
  `per_sentence` 367ms / 819ms / 13.76s, `whole_text` 5056ms / n/a (1 chunk) / 17.65s,
  `hybrid` 319ms / 3616ms / 15.31s, `hybrid3` 372ms / 2986ms / 15.27s,
  `inference_stream` 1064ms / 0ms / 13.29s, `buffered_stream` **615ms / 0ms / 11.86s**.
  All six transcripts came back complete (no truncation) with only expected STT
  artifacts (digit-vs-word "three"/"3", "STEP"→"step" casing). Audio saved to
  `voice_refs/audition/strategy_comparison/` (`scripts/compare_strategies.py`).
  **Listening verdict: `buffered_stream` set as `[voice].strategy`'s default** -
  matches `inference_stream`'s zero mid-response gaps while cutting TTFA nearly in
  half (1064ms -> 615ms), and came out fastest overall on total time; `hybrid3`
  sounded equally good but its ~3s gap is exactly what `buffered_stream` eliminates.
  New rule added (CLAUDE.md rule 6): any future TTS verification includes a
  transcript check, not just duration - duration ratios alone missed the truncation
  above; only transcribing the audio caught it.

- **Live end-to-end backchannel test - verified for real.** Real wake word, real
  trailing-off speech, real resume, via `python -m services.ears.pipeline` (had to
  rerun with `PYTHONUNBUFFERED=1` - the first attempt's printed transcripts never
  flushed to the captured output before the process ended, a tooling gotcha, not a
  pipeline bug). Confirmed directly from `logs/ears.jsonl` plus the actual printed
  `> ...` output: two full trail-off -> backchannel -> resume cycles, both correctly
  fused into one yielded utterance - one of them chained *two* resumes ("Oh, no."
  correctly did not read as complete on its own, kept waiting, then the real
  continuation arrived and got appended too). Rate limiting verified live: a second
  abandonment 17.5s after the first backchannel (under the 20s `base_cooldown_s`)
  correctly played no audio but still entered `awaiting_resume` and kept listening
  rather than silently giving up. Normal (non-abandoned) utterances still bypass the
  system entirely, confirmed on two more utterances. A same-score-across-consecutive-
  wake-triggers pattern looked like a caching bug at first glance; checked `wake.py`
  directly - the score is freshly computed from the model every call, no caching -
  the repeats just reflect the same phrase being said again, not a bug.

- Backchannel quality: four listening-driven fixes, none unilateral - every one
  regenerated and judged by ear before shipping (`scripts/regenerate_backchannel_pool.py`,
  `demo_level_ramp.py`, `demo_master_gain.py`, `demo_backchannel_vs_master.py`).
  1. **Non-lexical sounds dropped entirely.** First pass's generation prompt biased
     toward "Mm"/"Mhm"/"Hmm" - XTTS has no real pronunciation for these, they came
     out as long, strange vocalizations (1.9-2.2s for two letters) instead of a
     quick word. `_GENERATION_PROMPT` rewritten to require real lexical words
     ("Right.", "Yeah.", "Got it.", "Sure.", "Okay.", "I see.", "Oh?") and forbid
     non-lexical spellings and directives/questions alike - durations back to
     0.79-1.76s.
  2. **`soft` reference + `speed=0.88`, backchannel-only.** `BackchannelPool.
     ensure_filled()` saves the shared XTTS engine's active reference, switches to
     `soft` for the duration of the pool fill, restores it after (verified: active
     reference was `calm` before and after, in every regeneration run) - real
     responses never see the switch. Known gap, not addressed: no lock against a
     concurrent real `synthesize()` call landing mid-refill on the shared engine;
     low-probability given how short a refill is, flagged in `ensure_filled()`'s
     docstring for whenever it's worth revisiting.
  3. **Volume continuity across utterances** (`tts.py`'s `_ramp_gain()` /
     `_record_played_level()`, `[voice.level_ramp]`): without this, every utterance
     synthesized at a fixed level with no memory of the last one, so a soft
     backchannel could be immediately followed by a full-volume response - not how
     people talk. Tracks the RMS + timestamp of the last thing actually played
     (`play_audio()` and `_play_all()` both feed and read this shared state); if the
     next utterance starts within `window_s` (6.0s default), it's capped to at most
     `step_db` (6.0 default) louder than the last played level, computed once per
     utterance (from its first chunk) and held for the whole thing - recomputing per
     chunk would pump the level up/down mid-response instead of ramping smoothly
     turn to turn. Only ever attenuates toward continuity, never boosts. Verified
     with real synthesis/playback, not simulated: backchannel "Right." at -28.6dB ->
     turn 1 capped to -22.4dB (+6.2dB, matching `step_db`) -> turn 2 -19.8dB (barely
     capped, already near natural) -> turn 3 -18.6dB, fully recovered. Two to three
     turns to full recovery, as intended.
  4. **Master output gain** (`[voice].output_gain_db`, `tts.py`'s `_output_gain()`):
     one setting covering every playback path (`play_audio()`, `_play_all()` -
     backchannels and real responses alike), not per-strategy. Applied *after* the
     ramp, not before - the ramp reasons about relative levels between utterances
     via `_record_played_level()`, which records the pre-master-gain level so ramp
     comparisons stay in their own frame regardless of what the master is set to;
     the master is a pure final multiply with no feedback into ramp state. Compared
     -10/-20/-30dB on the same dry-synthesized response (`voice_refs/audition/
     master_gain_demo/`) - listening verdict: **-20dB**.
  Backchannel `volume_db` re-checked against the new master, since the two stack
  additively in dB: the old `-8.0` was calibrated against a 0dB master and measured
  10.6dB under a normal response once `output_gain_db=-20` landed - too buried.
  Also found a backchannel's *dry* level (before any `volume_db` attenuation at all)
  is already ~2.6dB under a response's, just from being short/soft-referenced/slow.
  Compared 0/-2/-4/-6/-8 stacked on the -20dB master (`voice_refs/audition/
  backchannel_vs_master/`) - listening verdict: **`volume_db = -2.0`**, landing
  about 4.6dB under a normal response.
  Two listening items closed this session, not left pending: the original
  temperature/speed param sweep on `calm_14` is superseded by the later
  `units_only`-text sweep and doesn't need a separate verdict; the `calm`-vs-`soft`
  reference choice is resolved by `soft` now being in real production use for
  backchannels specifically (fix 2 above) - no further decision needed on either.

- **A4 — closed the loop.** `services/brain/loop.py`: wake -> transcribe ->
  completeness check -> stream to LLM -> stream to TTS, running continuously as
  one conversation (`run()`), history tracked as a plain list, `config/persona.md`
  loaded verbatim as the system prompt. Barge-in wired through
  `pipeline.listen()`'s new `on_wake` callback (fires the instant a debounced wake
  event is detected, before verify/recording even start - the earliest signal
  available). First live pass found two real bugs, both from a naive first cut,
  neither hypothetical:
  1. `on_wake` cancelled the in-flight response task on *every* wake trigger,
     including the wake that starts the very next turn - silently killing a
     response that hadn't played any audio yet (still mid-LLM-stream). Fixed by
     gating cancellation on `tts.py`'s new `response_playback_elapsed_s()` (tracks
     when `_play_all()`'s first real chunk actually started playing) - only
     cancels if audio has been playing at least `[brain.barge_in].min_playback_s`
     (0.3s). Every decision (cancelled or skipped, and why) now logs to
     `logs/loop.jsonl`.
  2. A response task that dies (exception or silent hang) was only ever surfaced
     if something later `await`ed it - miss that window and asyncio just logs
     "exception was never retrieved" at GC time, easy to miss entirely. This is
     exactly how a turn could burn 24s and leave nothing in `brain.jsonl`.
     `response_task.add_done_callback(_on_response_task_done)` now logs every
     completion (ok/cancelled/error) unconditionally the instant it happens, and
     prints on error - can't fail silently again.
  `tts.py`'s `_write_interruptible()`: `sd.OutputStream.write()`/`sd.wait()` don't
  reliably preempt on `stream.abort()`/`sd.stop()` - reproduced a hung write
  thread surviving cancellation, joined at interpreter shutdown by asyncio's
  executor, crashing the process (access violation). Both `play_audio()` and
  `_play_all()` now write in ~100ms sub-blocks so `stream.abort()` has a real
  cancellation checkpoint instead of one for the whole chunk.
  With bugs 1-2 fixed, live testing surfaced a `srcIndex < srcSelectDimSize`
  device-side CUDA assert in GPT-2's embedding lookup during backchannel pool
  synthesis - two rounds of investigation, first one wrong:
  - Round 1 (text tokens): confirmed the tokenizer's vocab and the GPT's
    text-embedding table are consistent on this checkpoint (6681 both) -
    deliberate emoji/non-ASCII/empty-string probes against XTTS directly didn't
    reproduce it either. Built `_validate_text_tokens()` anyway (mirrors XTTS's
    own strip/lower/encode path, rejects any id outside the embedding table
    *before* the GPU sees it) since it's real defense-in-depth, plus
    `_CudaContextPoisoned` (a device-side assert corrupts the whole CUDA context,
    not just the one call - confirmed live, the cleanup `use_reference()` right
    after cascaded into further unrelated-looking errors - so every call after a
    fatal CUDA error now fails fast with one clear message instead of limping
    into more of those).
  - Round 2 (the real cause, found after the crash recurred with round 1's guards
    passing clean): not a length overflow either - 50 sequential real
    `synthesize()` calls on short backchannel-shaped text, including several
    genuine rambling outputs up to 5s long, never pushed the mel position index
    above 100 of the table's 608. Two threads calling `synthesize()` concurrently
    on the *same* engine instance reproduced a corrupted, negative
    (`-100`) mel position index on the first attempt - `GPT2InferenceModel.
    forward()`'s single-token decode step computes position from
    `self.cached_prefix_emb.shape[1]`, shared mutable state a concurrent
    `set_reference()`/`store_prefix_emb()` call can clobber mid-flight. This was
    exactly the "not locked against a concurrent real synthesize() call
    mid-refill" gap `backchannel_pool.py` had already flagged and assumed
    low-probability - it wasn't, and it's specifically why the crash showed up in
    backchannel synthesis (the pool refill is the one most likely to be running
    in the background when a real response's synthesis lands). Fix:
    `XTTSEngine._model_lock` (`threading.RLock`, not `asyncio.Lock` -
    `synthesize()` runs on `asyncio.to_thread` workers) now serializes every call
    touching shared model state (`synthesize`/`synthesize_stream`/
    `set_reference`/`use_reference`), across all three real call sites (per-
    sentence workers, the streaming path, backchannel pool).
    `backchannel_pool.py`'s `use_reference()` calls now go through
    `asyncio.to_thread` so waiting on that lock never blocks the event loop.
    Re-verified with the fix: the 2-thread repro now survives clean (30/30), and
    a harder 3-thread stress test firing all three real entry points at once (75
    calls total) also came back clean. Diagnosed both rounds by patching
    `torch.nn.functional.embedding` to bounds-check every lookup on the CPU side
    before it reaches the GPU - catches the same failure as a plain Python
    exception instead of a fatal assert, safe to iterate on without crashing the
    process each time.
  `onnxruntime` checked separately (CPU-only, `onnxruntime-gpu` not installed) -
  confirmed intentional-by-measurement, not another rule-8 silent misconfig: wake
  word detect measures 1.9ms median / 3.0ms max against a 50ms budget, 16-25x
  under target already, so there's nothing to gain from GPU here.
  Two more fixes landed after a further live pass ("four turns, no crash, clean
  Ctrl+C exit," then a follow-up conversation that hit real content issues):
  - **Chunk-cap-vs-normalize ordering.** `_split_into_capped_chunks()` was being
    called on *raw* streamed text, with `sanitize()`/`normalize()` applied to
    each already-capped piece afterward - but `normalize()` expands text ("1.2mm"
    -> "one point two millimeters", 5 chars to 30), so a raw chunk safely under
    the 350-char cap could balloon past XTTS's ~402-token hard limit after
    normalization. This was systemic, not one strategy - every
    `_split_into_capped_chunks()` call site (`whole_text`, `hybrid`'s remainder,
    `hybrid3`'s chunk 2/3, `buffered_stream`'s first chunk and remainder) had it.
    Fixed with `_normalized_capped_chunks()`: sanitize + normalize *then* cap,
    swapped in at every call site. Reproduced the exact failure numerically (a
    235-char raw chunk expanded to 429 post-normalization) and confirmed the fix,
    then verified end-to-end with real synthesis + transcription (rule 6): the
    resulting 263-char chunk transcribed complete.
    Re-investigated after a follow-up report that the warning fired again on a
    live FDM answer: extensive testing (real end-to-end `speak_stream()` runs
    against 4 different real LLM-generated FDM responses, plus a deliberate
    531-char no-punctuation run-on stress test) found **zero cap violations** -
    every chunk landed at or under 350 chars every time. The 250-char *warning*
    XTTS logs is a separate, lower, advisory threshold that's expected to fire on
    any chunk between 250-350 chars by design (350 was chosen with margin under
    the real ~450-char truncation boundary, not under XTTS's own more
    conservative 250-char log message) - seeing the warning alone was never
    evidence of a violation, confirmed again here. While testing this, found a
    real, separate, measurable anomaly worth a closer look in A5 or later:
    synthesizing the same FDM-topic content at graduated lengths (99/172/245/
    319/343 chars, same reference) showed a real pacing dip specifically at 245
    chars (12.05 chars/s vs. 17.32 and 13.94 chars/s on its neighbors, and 6+
    seconds of audio beyond what the surrounding pace would predict) - the same
    rambling/instability failure mode previously characterized only on short
    backchannel text can apparently also hit mid-length response chunks. Samples
    saved to `voice_refs/audition/chunk_length_accent/` for a listening verdict
    on whether this is what read as "British accent mid-sentence" - not
    confirmed, needs an ear, but it's a real, measured length-correlated anomaly
    independent of the truncation question.
  - **Acronym pronunciation.** `normalize.py`: all-caps 2-5 letter runs are
    spelled letter-by-letter with periods ("PLA" -> "P. L. A.") - bare-space
    joining ("P L A") still garbled on transcription, periods were load-bearing,
    not cosmetic. Small exception list (`NASA`, `OK`) for ones pronounced as a
    word. Tested PETG since it looked borderline on paper: it's not an
    exception - wrong both alone ("Try PETG instead." -> "Try PG instead.") and
    next to PLA ("Pele prints easier than peachy.") - spelled out it transcribed
    clean every time. Caught and fixed a bug in the fix itself: naive
    implementation double-punctuated at sentence end ("PLA." -> "P. L. A..") -
    fixed by checking the following character before adding a trailing period.
    `°C`/`F` temperature units and mixed-alnum extensions (`3MF`) are a known
    remaining normalization gap (pass through unconverted) - not yet fixed, no
    ticket for it yet.
  - **Response verbosity.** `persona.md`: explicit 1-2 sentence rule for factual
    questions, with the why (spoken vs. written - a listener can't skim ahead or
    skip to the part they wanted). Sanity-checked against the real LLM+persona:
    four different factual questions all came back in 2-3 sentences (including
    the mandated short-opener sentence) instead of paragraphs.
  `scripts/latency_report.py` (A4's spec deliverable) gained a `--until` option
  alongside `--since` - the log files accumulate ad-hoc diagnostic calls between
  live tests too (direct `engine.synthesize()`/`brain_client.stream()` calls made
  while debugging), which `--since` alone can't exclude from a report scoped to
  one specific live-test window.
  **Actual numbers, first time run end-to-end** (both real live-test windows,
  isolated via `ears.jsonl` wake-event timestamps so diagnostic-call noise from
  this same session couldn't leak in): wake word detect is the only stage inside
  budget (median 1.3-2.4ms vs. 50ms). Every other stage is over: VAD endpoint
  fixed at 610ms (this is `min_silence_duration_ms=600` plus overhead, not
  variance - CLAUDE.md already decided not to tune this, backchannel prompting
  covers the cost instead), STT 313-538ms median (vs. 300ms), LLM TTFT 681-811ms
  median (vs. 400ms - one outlier at 36.3s in the FDM window traced to a
  `keep_alive=30m` cold-reload after a 47.8-minute idle gap between test
  sessions, not a pipeline bug), TTS first chunk 1.4-2.8s median (vs. 200ms).
  Derived first-audio-out: 3.1-4.6s against the 1.15s target. A4's done-when
  ("three turns without feeling like waiting on a machine") is met subjectively,
  but the numbers say clearly why A5 (latency tuning) is next, not optional
  polish.

- **A5 in progress — TTS first chunk tightened, LLM TTFT diagnosed (not yet fixed).**
  `_consume_buffered_start`'s trigger loosened from 2-sentences-or-300-chars to
  1-sentence-or-150-chars (`_BUFFERED_START_CHAR_THRESHOLD` = 150) - NOT a repeat
  of A3's hybrid trade-off, since buffered_stream always fires its remainder as
  its own separate `inference_stream()` call regardless of the first-chunk
  trigger (hybrid's 3.6s gap came from waiting for the *entire rest of the
  response* as one call - a different failure mode this doesn't reopen).
  Measured on a real `speak_stream()` run (`scripts/compare_buffered_triggers.py`):
  572ms -> 394ms TTFA, same 0ms max gap. 1-sentence-alone (no char fallback)
  measured faster still (301ms) but reintroduced a small real gap (118ms) - built
  and saved (`voice_refs/audition/buffered_trigger_comparison/`) but not landed,
  pending a listening call on whether that gap is audible. Verified the landed
  change against a real production `speak_stream()` call after committing: first
  chunk correctly triggers on exactly one sentence, remainder streams at 0ms gap
  throughout.
  Separately confirmed, directly measured: the persona's short-first-sentence
  rule (originally justified by playback pipelining, A3) also cuts buffered_stream's
  trigger latency in its own right - same sentence 2, only sentence 1 length
  varied: a 7-char opener triggered at 237ms vs. 513ms for a 90-char opener. A
  second, independent reason to keep enforcing that rule, not just the original one.
  `latency_report.py` now splits "TTS first chunk" into "waiting for LLM text"
  and "engine synthesis" via `tts.py`'s new `synthesize_call` log records
  (`since_stream_start_ms`) - the old single number was conflating LLM generation
  pacing (buffered_stream waiting for its trigger condition) with actual TTS
  engine latency; diagnosed directly that ~1.5s of a typical 1.4-2.8s `ttfc_ms`
  was the former, not the latter.
  LLM TTFT (681-811ms live vs. bench.py's 382ms at 1K context, now the largest
  remaining gap) - real findings, not fully closed:
  - **Ruled out**: unbounded conversation history (real, `loop.py`'s `history`
    list never truncates or summarizes and every turn resends it whole - A6's
    "rolling context" isn't built yet - but TTFT measured *flat* across 5 real
    turns in both live-test windows, 656-811ms with no growth trend, so this
    isn't the live-vs-bench driver at session lengths tested so far). Persona
    system prompt alone (isolated test with the full persona + one user message:
    350-430ms, not 650+). Endpoint choice, `/api/chat` vs. `/api/generate` (both
    ~300-430ms in matched isolated tests - reconfirms A1's earlier "noise" finding
    independently).
  - **New finding, real and actionable**: Ollama's own `load_duration` field
    (now logged - `client.py`'s `_log_call` records `load_duration_ms`/
    `prompt_eval_count`/`prompt_eval_duration_ms` from the final chunk) is
    *not* a cold-start-only signal - it measured ~285-335ms on every single
    call tested, warm or cold, chat or generate endpoint. `ttft_ms` (client-
    measured) matches `load_duration_ms + prompt_eval_duration_ms` (Ollama-
    reported) to within ~25ms consistently. This means roughly 300ms of *any*
    call's TTFT, including bench.py's own 382ms baseline, is a fixed Ollama-side
    floor - not something reducible from our side without a different Ollama
    version/model/GPU-scheduling setup.
  - **Real but modest**: concurrent XTTS synthesis (matching a backchannel-pool
    refill or overlapping turn) adds ~35-40ms to TTFT directly (323-343ms ->
    376-383ms, isolated test) but roughly *halves* generation throughput
    (~124 tok/s -> ~58 tok/s) - a bigger hit to total response duration than to
    time-to-first-token specifically.
  - **Not fully closed**: combining persona + realistic 5-turn history +
    concurrent GPU load in one isolated test reached 395-587ms - closer to the
    live 650-811ms range but still short of it. The remaining gap wasn't
    pinned down this session (real conversation history is likely longer/more
    varied than the synthetic 5-turn test, and the full live pipeline runs more
    concurrent CPU/GPU activity - wake word, VAD, STT, backchannel pool - than
    any isolated reproduction captured). `client.py`'s new logging means the
    *next* real live session will show the real `load_duration`/`prompt_eval_duration`
    breakdown directly instead of requiring more reproduction guesswork.

- **Persona: character brief written (`config/persona.md`), two known limitations
  found and closed out rather than left open-ended.** Response shape (1-2 sentences,
  short first sentence, no editorializing closers), correction precedence (a
  correction to her own claim is taken flat, never disputed), and disagreement
  grounds (suspicion + question, never an invented specific number) are all written
  and verified live against real `gemma4:e4b` calls.
  **Known limitation: dry wit doesn't reliably fire.** Tested two different ways -
  a repeated-question scenario (meta-awareness of being asked N times) across five
  conditions (full persona, a stripped third-size persona, positive examples in the
  shape rules, a literal third-ask matching the sample line's own framing, and
  `gemma4:12b` instead of `e4b`), 25 runs, zero firings in any condition - and a
  content-only scenario (the dry aside is about the answer's content, not the
  exchange - four factual questions x5 runs, full persona, text-only), zero firings
  there too. Ruling out both prompt structure and model size, and ruling out that
  the first test's meta-awareness framing was just a harder bar than dryness itself
  requires, means this isn't a test artifact - the trait is genuinely close to absent
  under this persona/model combination. Not chasing it further. One incidental
  counter-data-point from the padding retest below: 2 of 20 responses there landed a
  genuine dry aside unprompted ("enough time to grab a coffee, but not enough to
  decide what you'll work on next") - so the trait *can* fire, just rarely, and not
  reliably from any rule change tried so far.
  **Known limitation: unsolicited padding (volunteered advice/warnings/next-steps
  beyond what was asked) holds at roughly 2/3 of responses regardless of how the
  rule is worded.** Same 20-prompt content-only test, same responses used for the
  dry-wit check above: a negative-prohibition version of the rule ("she answers only
  what was asked") measured 10/15 responses (excluding one prompt set that mostly
  produced clarifying questions instead of answers, not padding) volunteering
  unrequested checks/warnings/next-steps; rewriting it as a positive definition
  ("a complete answer contains the information asked for and nothing else, and
  never includes what to check/watch for/do next/might go wrong unless asked")
  measured 9/15 on an identical retest - flat, within noise, not a fix. Two
  different rule phrasings converging on the same rate is real evidence the lever
  isn't persona wording - this model pads by default on these prompts and the
  persona can't reliably stop it. Not re-fixing via a third rewording; if this
  needs solving it's a different kind of fix (e.g. a post-generation trim pass)
  worth scoping deliberately, not another prompt tweak.

- **A6/A7 — Memory, hand-rolled (`services/memory/`), not Letta.** PLAN.md's Phase 2
  named Letta specifically; investigated first rather than assuming it fit (every
  third-party integration here has had real dependency friction). Two real findings,
  full reasoning in PLAN.md's Phase 2 section: `letta` (the full server) has no numpy/
  torch/transformers conflict, but installed into this project's shared venv it
  silently downgrades `onnxruntime` 1.28.0->1.20.1 (openWakeWord's dependency - the
  hand-tuned wake-word calibration sits on top of this), plus `protobuf`/`wrapt` -
  fixable by isolating it in its own venv behind `letta-client`, but the real
  disqualifier was underneath that: Letta's core design is MemGPT-style, the agent
  manages memory via its own LLM function-calls, adding per-turn round trips this
  project's latency budget doesn't have room for. The actual spec (profile always-
  injected, summarize-at-70%-fill, top-k retrieval) is three deterministic steps, not
  an autonomous agent - built directly instead, matching how every other integration
  here has gone (no LangChain in A8's agent loop either).
  Three layers: `services/memory/profile.py` reads `config/profile.md` verbatim into
  every turn's system prompt (new file, hand-editable template, deliberately no
  invented facts about the user - populate it yourself). `services/memory/manager.py`'s
  `MemoryManager` owns rolling context - `services/brain/loop.py` no longer resends an
  unbounded history list; `[models].context_window` (new, 16384) gives every call a
  fixed, config-driven `num_ctx` instead of Ollama's unconfigured 4096 default, needed
  because persona.md alone measured 3295 real tokens (via a live call's
  `prompt_eval_count`) - 80% of that old default before profile/retrieval/history even
  entered the picture. At `[memory].rolling_fill_threshold` (0.70) the oldest
  `rolling_chunk_messages` (4) raw turns fold into one live, evolving summary via
  `services/memory/summarize.py`, always at least `rolling_min_recent_messages` (4)
  kept raw. Retrieval (`services/memory/store.py`, sqlite-vec) embeds every message via
  `nomic-embed-text` over Ollama (`services/memory/embeddings.py` - fully local, no
  cloud key, confirmed via source read of Letta's own Ollama provider that this is the
  same mechanism it would have used) the instant it's known, and pulls
  `[memory].retrieval_top_k` (8) nearest fragments into the system prompt each turn.
  VRAM measured directly (nvidia-smi, real calls, not estimates): embedding model
  resident alongside the full e4b+Whisper+XTTS stack leaves ~1956MB free - comfortable;
  adding a resident fast chat model on top of *that* drops it to 585MB, too thin given
  this project's own prior finding that a similar margin (217MB, `gemma4:12b`) still
  regressed a later call to a 6.1s reload. So summarization uses `[models].primary`,
  not a resident fast model, and always fires as a background `asyncio` task
  (`MemoryManager.spawn_after_turn()`) after `speak_stream()` has already returned -
  never adds latency to the response the user is hearing.
  Caught and fixed a real bug during end-to-end verification, not left for later: a
  session's very last message was being silently lost, because storage fires as a
  fire-and-forget background task and `asyncio.run()` tears down the event loop the
  instant `_main()` returns - an in-flight task at that exact moment is orphaned, not
  awaited, not an error either. Reproduced live (session 2's final reply never reached
  disk), fixed with `MemoryManager.drain()` (awaits all pending background tasks, 10s
  timeout) wired into `loop.py`'s shutdown path before `brain_client`/embeddings/store
  close - re-verified clean afterward, all 8 entries across both sessions present.
  **Done-when verified for real**: two genuinely separate `uv run python` process
  invocations (a real restart, not a fresh in-process object) - process 1 stated two
  facts (a pet's name/breed, a corrected deadline), process 2 (fresh `MemoryManager`,
  empty turns/summary) answered both correctly, sourced purely from retrieval against
  disk. `scripts/memory.py` (A7's inspector, built alongside the memory layer per
  PLAN.md's explicit instruction, not after) confirmed which session and entry each
  answer came from - `list`/`sessions`/`show`/`delete` all exercised against that real
  data, `edit-profile`'s backup-before-edit mechanism verified (diff-on-change,
  silent no-op when nothing changed, no `.bak` left behind either way).
  Test/fixture data (the corgi/Henderson facts) cleared from the real store afterward -
  `memory_store/` is gitignored, `config/profile.md` ships as an empty hand-fillable
  template, not backfilled with anything invented about the user.

- **A8 — Agent loop (`services/brain/agent.py`, ~170 lines) + read-only tools
  (`tools/`), hand-rolled, not LangChain.** While-loop over `messages` with a
  dispatch dict, matching PLAN.md's own reasoning - `services/brain/client.py`
  already passed tools through in OpenAI format and yielded a `tool_calls` chunk
  as JSON (A1, smoke-tested), so this module really was the only new machinery,
  same as PLAN.md predicted. Max 10 iterations, hard cap. Every tool call logged
  (`logs/agent.jsonl`) with arguments and result, never anything a tool holds
  internally - see below for why that mattered concretely, not just in theory.
  Tool dispatch keyed on active model (`[tools.by_model]` in cortana.toml, empty
  = all tools) - no models need different sets yet, but the seam exists for A9's
  confirmation-gated write tools without restructuring this loop.
  Four tools built: `fetch_url` (httpx + trafilatura), `read_file`/`list_dir`
  (whitelisted to `[tools].whitelist_dirs`, path-traversal-safe resolution
  factored into `tools/_fs.py` so the security-relevant check exists exactly
  once), `web_search` (see below). `[tools].max_result_chars` (3000, shared
  across every tool via `agent.py`'s dispatcher, not duplicated per-tool) caps
  what any single tool result folds back into the conversation - `[models].
  context_window` is 16384 and persona.md alone is already 3295 tokens (A6), so
  an uncapped `fetch_url` could singlehandedly trip A6's rolling-context
  compression or blow the window outright.
  **`web_search`: built, both backends, not currently offered to the model.**
  Investigated Tavily first (real key required) then switched to SearXNG
  (self-hosted, zero external calls/keys) by explicit choice - but this machine
  has no Docker, and installing SearXNG bare into WSL2 was more setup than
  wanted right now. Rather than leave a tool in the list that can only fail,
  `tools/web_search.py` gates itself live: `is_available()` checks
  `TAVILY_API_KEY`/pings the SearXNG endpoint (1s timeout, fails fast, not a
  one-time-at-startup latch) every `run_agent()` call, and `agent.py` drops
  `web_search` from the offered tool list when it returns false, logging why.
  The moment either backend becomes real, it's offered automatically - no code
  change. `tools/_search_tavily.py`/`_search_searxng.py` implement the same
  `search(query, max_results) -> list[dict]` interface underneath, same
  config-driven swap pattern as `[voice].engine`. Confirmed structurally, not
  just by intent, that the Tavily key can never reach `logs/agent.jsonl`: the
  key is used only as an outbound `Authorization` header inside
  `_search_tavily.py` and never appears in that function's return value, and
  `agent.py`'s logger only ever records tool *arguments* (the model's own call,
  e.g. `{"query": ...}`) and the *result* text - never anything a tool module
  holds internally.
  **A8's original done-when ("what's the weather in Miami" triggers a search) is
  not met - a deliberate deferral, not a broken build.** Revised, agreed as
  arguably the better test anyway (multi-step, a real whitelisted directory, no
  external dependency so a failure means the loop is broken rather than the
  network): "read the config file and tell me what the wake threshold is."
  **This did not pass on the first real attempt, and the investigation is the
  actual finding here, not the final green run.** First real test (`think=false`,
  matching the voice loop's default): the model asked for a path instead of
  exploring - a static, generic tool description ("only whitelisted directories
  are accessible") gave it nothing to discover the whitelist *from*. Fixed by
  making tool specs computed (`spec()` functions, not static `SPEC` dicts) so
  `list_dir`/`read_file`'s descriptions interpolate the real, current
  `whitelist_dirs` - the model can now see "config, logs, cad" directly in the
  tool schema instead of needing to already know it. That alone wasn't enough:
  re-tested 5x, only 2/5 produced a correct tool-call chain - the rest narrated
  intent without ever calling a tool ("I need to list the files first"), stalled
  after a partial chain, or once called `read_file` on `config/settings.txt`, a
  filename that doesn't exist and didn't appear anywhere in its own preceding
  `list_dir` result. Added an explicit tool-use system instruction
  (`_TOOL_USE_INSTRUCTION` in `agent.py`, merged into the system message when
  tools are offered) - measurably better but still not reliable (2/5 clean,
  including one case that skipped tool use entirely and got the right number by
  what can only be a lucky guess, since the real value was never retrieved).
  Tried `think=true` last, since `[thinking]` already has this exact per-call-site
  pattern (CAD/heavy already default true) - **5/5 correct tool-call chains, 5/5
  correct final answers**, re-confirmed 3/3 more through the real code path (not
  a monkeypatch) after wiring it in as `[thinking].agent = true`. Cost is real
  and measured, not hand-waved: ~1.2-3.5s per call vs ~0.8-1.7s with thinking
  off, and a full turn makes 2-3 calls - accepted because `agent.py` isn't wired
  into the low-latency voice loop yet, worth re-examining the moment it is.
  Model-side validation held throughout, as PLAN.md predicted it would (e4b was
  checked against this exact workload - 6 selection cases plus a real
  multi-step chain - before the switch from 12b, matching `gemma4:12b` exactly
  on every case) - every failure mode found here was a *reliability* problem
  (narrating instead of calling, losing track mid-chain, occasionally
  hallucinating a filename), not a *capability* one (wrong schema, invented
  tool, malformed JSON) - `think=true` closing it out this cleanly is consistent
  with that being a shallow generation-discipline gap, not a hard ceiling.

- **A9 — Write tools + confirmation gate + abort hotkey + no-credentials rule,
  all dispatcher-enforced code, not persona instructions.** A5a already
  measured negative persona constraints holding at roughly two-thirds
  reliability (padding investigation) - not remotely a safety bar, so none of
  A9's guarantees live in prompt text.
  `services/brain/agent_safety.py` (new): `confirm()` blocks on real stdin
  input (`asyncio.to_thread`, timeout treated as declined, not hung forever) -
  **keyboard-only, stated plainly, not pretended otherwise**: `agent.py` runs
  standalone, with no wiring yet to `services/ears/pipeline.py`'s mic/STT
  path, so there is no way for a spoken "yes" to reach this dispatcher until
  that wiring happens. `credential_violation()` scans every tool call's
  arguments (key-name and value-shape heuristics) and refuses outright before
  any tool code runs - real backstop against a future tool built with a
  credential-shaped parameter, or the model echoing something secret-looking
  it picked up from `fetch_url` (A8) back into a call. Both gates live in
  `agent.py`'s `_call_tool()`, ahead of execution, unconditionally - a tool
  flags `REQUIRES_CONFIRMATION = True` as metadata, but the actual blocking
  is dispatcher code, never something the model can talk past.
  Global abort hotkey (`ctrl+shift+x` default, `keyboard` package's low-level
  Windows hook - confirmed installs without admin rights here) cancels
  whatever tool call is in flight via `call_soon_threadsafe` cross-thread
  task cancellation, same bridging pattern `services/ears/pipeline.py` already
  uses for the mic callback. Verified: the cancellation logic itself (task
  correctly raises `CancelledError` when the callback fires) - **not** a real
  physical keypress, which this environment can't simulate. Worth a real test
  the first time someone's actually at the keyboard.
  `write_file` (whitelisted to `[tools].write_whitelist_dirs` - deliberately
  separate from read's `whitelist_dirs`, empty by default so nothing is
  write-authorized until explicitly configured) verified end-to-end for real:
  declined via the confirmation prompt (no file written), confirmed but
  outside the whitelist (rejected, no file written), and confirmed inside a
  temporarily-added whitelist entry (file written correctly, then the config
  change reverted).
  **`shell`: built against a real, working containment mechanism - not the
  one first considered.** No Docker on this machine, but WSL2 already was
  (an existing "Ubuntu" distro) - initially proposed running whitelisted
  commands there with argument-level validation blocking `/mnt` paths, but
  the user correctly rejected this: a blocklist only has to have one gap, and
  the existing distro is used for other things, so its automount can't safely
  be disabled anyway. Landed on a **dedicated, isolated WSL2 distro**
  (`CortanaShell`, imported fresh via `wsl --import` from a minimal
  `ubuntu-base-26.04` rootfs) with automount disabled in its own
  `/etc/wsl.conf` - filesystem-enforced isolation, not application-level
  filtering: `/mnt/c` doesn't exist inside it at all, the same guarantee a
  container's filesystem namespace gives. `tools/shell.py` invokes via
  `wsl.exe -d CortanaShell -e <command> <args...>` - `-e` skips the default
  Linux shell, and the args stay a real argv list the whole way down (Python's
  `asyncio.create_subprocess_exec`, never a joined string), so shell
  metacharacters in an argument are inert data, not syntax. **Confirmed live**
  with a deliberately hostile argument (`"hello; rm -rf / #..."` passed to
  `echo`) - printed back as one literal string, not executed as a second
  command. `is_available()` checks, fresh every call: does the distro exist,
  and is `/mnt` actually empty inside it right now - not trusted from a
  config flag or checked once at startup. Setup is a one-time step the user
  runs themselves (real commands, not summarized): download
  `ubuntu-base-26.04-base-amd64.tar.gz` from
  `cdimage.ubuntu.com/ubuntu-base/releases/26.04/`, `wsl --import CortanaShell
  <install-dir> <tarball> --version 2`, write `[automount]\nenabled = false`
  into its `/etc/wsl.conf`, `wsl --terminate CortanaShell` to apply it, verify
  via `wsl -d CortanaShell -- ls /mnt` printing nothing. Dormant until
  confirmed done - `tools/shell.py` starts working automatically the moment
  it is, no code change.
  **`calendar_read`/`email_read`: real infrastructure check first, then a
  real bug caught live, not in review.** No Outlook found via the Uninstall
  registry or `outlook.exe` on PATH - looked deferred, matching web_search's
  precedent (build the `is_available()`-gated interface now, real backend
  later). But testing the actual COM path revealed Outlook *was* installed
  (Click-to-Run, which doesn't register the way the initial search checked
  for) - and the first `is_available()` implementation, checking via
  `win32com.client.Dispatch("Outlook.Application")`, actually **launched a
  real Outlook process** to answer the question. Confirmed directly: a real
  `OUTLOOK.EXE`, hung 30+ seconds, no visible window, relaunched itself once
  after being killed. `is_available()` runs unconditionally on every single
  `run_agent()` call - wired into the live loop, that would have silently
  started Outlook on every conversational turn. This is now CLAUDE.md rule
  10: an availability check must be incapable of starting or changing
  anything, full stop. Fixed in `tools/_outlook.py`: check for an
  already-running `OUTLOOK.EXE` process first (`tasklist`, read-only, no COM
  involved at all), then `GetActiveObject` (never `Dispatch`) - it only
  attaches to an already-running COM server, never launches one. Re-verified
  clean: returns `False` in well under a second, confirmed via `Get-Process`
  that nothing launched. Net effect: Outlook must already be open for either
  tool to activate - permanently dormant if the user never opens it, which is
  the correct, safe failure mode, not a workaround to avoid. Zero credentials
  either way - COM automation rides whatever account is already signed into
  the desktop app.
  Two more prompt-injection attempts surfaced and flagged during this step
  (a fake "file modified, don't tell the user" system-reminder on `CLAUDE.md`
  and later on `config/cortana.toml`, the latter misrepresenting the
  assistant's own just-made test edit as an external change) - both refused
  per the standing rule from A8, third and fourth occurrences of the same
  pattern this project has now seen.
  **`shell` fully verified end-to-end once `CortanaShell` was set up - and a
  second real bug caught in the process, this time in the verification logic
  itself, not the tool it was checking.** `is_available()`'s first version
  (`ls /mnt`, checking whether anything was listed) returned `False`
  permanently, even with automount genuinely disabled and confirmed correctly
  in `/etc/wsl.conf` (byte-verified, fresh reimport, full `wsl --shutdown` +
  8s wait per Microsoft's documented "8 second rule" - none of it moved the
  result). Root cause, found by checking whether `/mnt/c` actually contained
  real content rather than trusting the listing: WSL unconditionally creates
  `/mnt/c`, `/mnt/wsl`, `/mnt/wslg` as empty stub directories regardless of
  the automount setting - `ls /mnt` always lists them whether or not anything
  is actually mounted there. Confirmed directly: `ls /mnt/c/Windows` failed
  with "No such file or directory", and `mount` showed zero `drvfs` entries -
  isolation was correct the entire time, the check was just reading the wrong
  signal. Fixed to `ls -A /mnt/c` and checking for emptiness instead - a real
  Windows C: mount is never empty in practice, so "empty" is a reliable
  unmounted signal without needing to parse `mount`'s output format. Re-ran
  clean: `is_available()` correctly flips to `true`.
  With that fixed, full pipeline verified for real, not just the gate in
  isolation: a whitelisted command (`echo`) through the real dispatcher -
  confirmation prompt shown, confirmed, executed inside `CortanaShell`,
  correct output returned (exit 0). A decline (`whoami`, answered "n") -
  correctly not executed. And, the sharpest test: `rm -rf /`, confirmed by
  the user at the prompt, still rejected by the whitelist check inside
  `execute()` - proof the confirmation gate and the whitelist are independent
  layers, not one gate that a "yes" bypasses entirely.

- **A10 — `ask_user` as a real callable tool, plus a real cross-module bug it
  exposed.** Genuinely "returned through TTS," not just printed: `execute()`
  speaks the question aloud through the same engine every response uses
  (`services/voice/tts.py`), verified live (real XTTS synthesis and
  playback). **The answer side is a known, explicit deferral, same category
  as `web_search` (A8) and `calendar_read`/`email_read` (A9): keyboard-only
  until `agent.py` is wired into `services/brain/loop.py`'s live conversation
  loop, because there is currently no path at all from a spoken word to this
  process** - `services/ears/pipeline.py`'s mic/STT output isn't connected to
  `agent.py` yet. Not a placeholder pretending to work; stated plainly.
  `persona.md` governs *when* asking is right (irreversible actions, missing
  filenames/dimensions, genuine ambiguity - guessing is the failure there,
  not asking); the one-question-per-turn *cap* is enforced in `agent.py`'s
  `run_agent()` by counting real calls, not trusted to the model - consistent
  with A9's whole premise (negative persona constraints measured at roughly
  two-thirds reliability). Verified live and adversarially: a genuinely
  double-ambiguous request ("write a settings file with the print
  temperature and filename") got the model trying to ask again 6 more
  times after the first real question - every single one correctly blocked
  by the dispatcher (`ask_user_cap` logged at counts 2 through 7), never
  reaching a second real execution. The model's own fallback behavior when
  blocked wasn't graceful (it kept trying to ask rather than proceeding on
  its best judgment, eventually just repeating the question as prose) - a
  model-reliability finding in the same family as A8/A9's (mechanism holds,
  synthesis quality is the separate, already-documented soft spot), not a
  dispatcher bug.
  **Real bug, found live, not obvious from either module in isolation**: the
  first full-loop test produced an `EOFError: EOF when reading a line` inside
  `ask_user.execute()`'s `input()` call - reproducible, but the *isolated*
  tool worked perfectly every time. Root cause was in a completely different
  tool: `tools/shell.py`'s `_run_wsl()` subprocess call (used by
  `is_available()`, which runs on every single turn per rule 10) didn't set
  `stdin=DEVNULL`, so it inherited the parent process's stdin handle by
  default - and silently consumed/closed the piped input meant for
  `ask_user`'s later, unrelated `input()` call. Fixed in `_run_wsl()`, and
  the same latent issue fixed defensively in `tools/_outlook.py`'s
  `tasklist.exe` call (grepped the whole tree for other
  `create_subprocess_exec` call sites - only those two existed, both now
  explicit). Re-verified clean after the fix: real question spoken, real
  answer captured, correctly used in the final response (asked material,
  got "PETG," answered "230 degrees Celsius" - genuinely correct for that
  filament, not a coincidence like A6's "0.5" guess).
  **Follow-up: factored the actual keyboard-input mechanism out to
  `services/brain/user_input.py`, shared by `agent_safety.confirm()` and
  `ask_user.execute()` - without merging their semantics.** `confirm()` is a
  dispatcher-enforced gate that stops an action already decided (the model
  can't route around it); `ask_user` is the model choosing to ask a question
  before deciding anything, a normal tool call with no gate behavior. They
  were sharing the *shape* of their input handling (both doing their own
  `asyncio.to_thread(input, ...)`) without sharing the code, which meant two
  places to update instead of one. `user_input.py`'s `get_answer()` is the one
  real mechanism now; `set_input_handler()` swaps it for a voice-based
  callback at the loop-integration step - a callback change for both callers
  at once, not a redesign of either, and not built twice now for the same
  reason A9's gate stayed keyboard-only rather than getting its own separate
  voice path early. Verified live: both real end-to-end paths still work
  through the shared plumbing (a real `write_file` confirmation, a real
  spoken `ask_user` question with a typed answer), and a swapped fake handler
  transparently changed both `confirm()`'s and `ask_user()`'s behavior with
  zero edits to either call site.
  **`is_available()`'s emptiness check turned out to be a second, subtler
  version of the exact bug it was fixed to avoid - both prior versions
  inferred mount state from `/mnt`'s directory contents instead of asking the
  kernel directly.** User set up `CortanaShell`, confirmed isolation by hand
  (`mount` showed zero `drvfs` entries - the genuine ground truth), and found
  `/mnt/c` itself was an empty leftover directory from the distro's first
  boot, since removed. The `ls -A /mnt/c` emptiness check (this session's
  prior fix) happened to return the right answer either way (empty or
  missing both read as "unmounted"), but it was still an inference about
  what a directory happens to contain, not a check of whether anything is
  actually mounted - the same category of mistake as the original `ls /mnt`
  version, just one layer more careful. Rewritten to check `mount`'s own
  output for a `drvfs` entry - `drvfs` is the filesystem type WSL uses for
  every Windows-drive mount, so its total absence is a direct answer, not a
  proxy for one. Re-verified the full A9 pipeline against the confirmed-clean
  distro: `is_available()` → `true`; a whitelisted `echo` through the real
  dispatcher → confirmed, executed, correct output; a decline (`whoami`) →
  correctly not executed; and a combined hostile-argument test (`cat` on
  `"/mnt/c/Windows/win.ini; rm -rf / #"`, confirmed by the user at the
  prompt) → `cat: '/mnt/c/Windows/win.ini; rm -rf / #': No such file or
  directory` - the entire string, `;`/`#` included, stayed one literal
  filename argument (never shell syntax) *and* the Windows path genuinely
  doesn't exist inside the distro, both defenses holding at once against a
  single adversarial input.

- **A11 — Proactive daemon (`services/daemon/daemon.py`), the orchestrator on
  top of scaffolding that was already there.** Found before writing anything:
  `services/daemon/{timers,calendar_trigger,email_trigger,relevance,output}.py`,
  `services/voice/playback_state.py`, and `tools/set_timer.py` (already wired
  into `agent.py`'s dispatch) all existed already, landed in an earlier
  undifferentiated commit - real, working building blocks (`daemon_store/
  timers.json` already held one previously-fired real test timer) but nothing
  polled or connected any of them. `daemon.py` is that missing piece: poll
  every source -> quiet hours -> rate limit -> relevance filter -> wait for
  playback -> announce, run continuously as `python -m services.daemon.daemon`.
  **Coexistence decided before building**, per the explicit instruction to
  state the approach first: both processes are plain HTTP clients to the same
  already-running Ollama server (no second model load, same reasoning
  `[models]` already relies on for the conversation loop). Voice output can't
  share that way, though - a second `XTTSEngine` in a second process would
  load a second full checkpoint onto the GPU (confirmed by reading
  `xtts_engine.py`'s constructor - unlike the LLM, there's no inference server
  in front of TTS). So `daemon.py` never imports `services.voice.tts` at all,
  only the dependency-free `services.voice.playback_state` cross-process flag
  file that module was already built for - `_wait_for_playback()` polls
  `is_active()` and holds off announcing until it goes false (bounded by the
  new `[daemon].max_playback_wait_s`, 60s default, so a stuck flag delays one
  announcement instead of silencing the daemon forever), rather than racing
  or cancelling anything mid-response.
  Relevance filter (`relevance.py`, already written) follows the same
  calibration lesson A10's ask_user needed: enumerated yes/no categories with
  worked examples, not "use your judgment." Cheap gates (quiet hours, rate
  limit) run before the LLM relevance call, not after, so a call already
  headed for suppression doesn't cost one. Added `[thinking].daemon = false`
  (a single yes/no classification isn't the multi-step tool-chain case that
  justified `agent=true`'s think=true default - untested against a live
  misfire, worth revisiting if one turns up) and `[daemon].max_playback_wait_s`.
  Candidate dedup (`announced_ids`, an in-memory set scoped to the daemon
  process's lifetime) marks a candidate seen the instant it's found, whether
  or not it ends up announced - same "the event itself is the mark" reasoning
  `timers.py` already applies to its own store. Matters most for
  `calendar_trigger`/`email_trigger` once they're live: without this, an
  event still inside the lookahead window would re-surface as a fresh
  candidate on every 30s poll cycle until it actually started. Rate limiting
  is in-memory too, resets on restart - accepted given the default's already
  conservative (2/hour) and this isn't an observed problem, not a persisted
  mechanism.
  **Verified end-to-end, real Ollama calls, not mocked**: set a real timer via
  the real agent-callable `tools/set_timer.execute()` (same path `agent.py`'s
  dispatch would use - true voice-set-to-daemon-fired is still gated on voice
  reaching `agent.py` at all, the same deferral A8/A9/A10 already documented,
  not something this step changes), injected a deliberately trivial fake
  trigger ("Weather update: partly cloudy...") alongside it, ran one real
  `_tick()`. The timer candidate passed relevance and reached
  `output.announce()`; the trivial candidate was correctly suppressed
  (`logs/daemon.jsonl`: `{"stage": "suppressed", "reason": "not_relevant",
  ...}`) and never announced. Also smoke-tested the real standalone entry
  point (`python -m services.daemon.daemon`) against the real config - clean
  start, correct real quiet-hours/rate-limit values logged, no crash from the
  dormant `calendar_trigger`/`email_trigger` `is_available()` checks running
  on a machine with no Outlook open. Test timer artifacts cleared from
  `daemon_store/timers.json` afterward, same as A7's memory-store cleanup.
  **Known limitation, stated plainly, same category as A8/A9/A10's
  deferrals**: `output.announce()` is CLI-only (`[DAEMON] ...` to stdout) -
  there is no path yet from this process into the live voice loop's TTS
  output, since `loop.py` isn't wired to accept proactive interjections and
  this daemon deliberately doesn't own its own TTS engine (see above).
  `output.set_output_handler()` is the swap point, same shape as
  `user_input.py`'s precedent - added explicitly to the loop-integration
  step's list of things to close out, alongside A9's confirmation gate and
  A10's ask_user answer path.

- **A12 — Control panel UI (`ui/`), rebuilt in TypeScript with the real
  visual direction (blue holographic, frameless, custom chrome).** First
  pass (plain JS, default Electron chrome) landed, then was superseded in
  the same session by an explicit follow-up spec: TypeScript (the codebase's
  Python/TypeScript split extends to this too, not just prose), a frameless
  window with a hand-built title bar/drag region/window controls,
  semi-transparent glass panels over `backdrop-filter: blur()`, and reused
  latency math instead of raw-log summing. The plain-JS version's files were
  replaced outright, not layered on.
  **Transport, stated before writing UI code (explicit requirement)**: still
  no WebSocket/HTTP server on the Python side - `ui/src/log_tail.ts` upgrades
  from A12v1's interval-only polling to `fs.watch()` on each log's containing
  directory (a real OS-level push notification the instant Python's
  `open(...).write()` appends a line) plus a slow backstop poll for whatever
  `fs.watch` misses (a known gap, particularly on Windows). That's the "push
  channel" the live tool-call indicator needs without inventing a second
  transport alongside the file-based one `services/voice/playback_state.py`
  already established (A11). Verified the push is real, not just the
  backstop coincidentally firing fast: a headless `node` test wrote a line
  and measured notification in 191ms against a 5000ms backstop interval.
  **Reused, not reimplemented, per explicit instruction**: `scripts/
  latency_report.py` refactored into `compute_report()` (the corrected
  critical-path list, `_split_ttfc`'s ttfc_ms double-counting fix, the
  target/status logic - all exactly as before) called by both the existing
  printed CLI table and a new `--json` flag - one implementation, two
  outputs, not a parallel TS port of the math. `ui/src/py_bridge.ts` is the
  one shared shell-out helper both the latency panel and memory tab use.
  **Memory inspector: edit and delete now genuinely work, not just view**
  (explicit requirement - A7 built this inspector specifically for drift
  correction). `services/memory/store.py` gained `update_passage()` -
  re-embeds via `services/memory/embeddings.py` when given a vector, so
  retrieval matches the corrected text rather than a stale vector for text
  that's since been fixed. `scripts/memory.py` gained `edit ID --text
  "..." [--no-reembed] [--json]`, and `delete` gained `--json`. Real
  dispatcher-safety decision, not incidental: `--json` mode refuses to
  delete without `--yes` rather than ever calling `input()` - a spawned
  child process (this UI) has no real stdin to answer an interactive prompt,
  the same `EOFError` class of bug A10 already hit once with
  `tools/shell.py` + `ask_user` (CLAUDE.md). Verified end-to-end against a
  real store, not mocked: inserted two real passages, edited one through the
  actual UI (screenshot-confirmed text change), deleted the other through
  the actual UI, then independently re-queried via the CLI (not the UI's own
  view of its own write) and confirmed exactly one passage remained with the
  edited text - the delete and the re-embed both genuinely persisted.
  **Frameless/transparent chrome**: `frame: false` + `transparent: true`,
  custom title bar built in `index.html`/`style.css`
  (`-webkit-app-region: drag` on the bar, `no-drag` on the window-control
  buttons and badge), IPC-driven minimize/maximize/close since a frameless
  window gets zero OS-provided controls. `[ui]` added to `cortana.toml`
  (`panel_opacity`, `blur_px`, `accent`) - opacity is a tunable per explicit
  instruction ("so I can tune it rather than guessing once"), read once at
  startup via the same "shell out to real Python TOML parsing, don't hand-roll
  a second one in JS" pattern the rest of `ui/` already uses.
  **Two real bugs found only by driving the actual rendered app, not by
  "no exceptions in the terminal"** (rule 6, and this project's established
  "an Electron window can't be inspected except by rendering it" problem -
  solved differently this time, see verification below):
  1. `tsc` compiled `renderer.ts` to CommonJS (`Object.defineProperty(exports,
     ...)`) because the base `tsconfig.json`'s `module: Node16` applied to
     every file in `src/`, but `renderer.js` loads via a plain `<script>` tag
     in the browser, which has no `exports` global - the very first line
     threw `ReferenceError: exports is not defined` and silently killed the
     *entire* script. Every symptom (empty conversation panel, unmoved model
     badge, unresponsive buttons) traced to this one throw, not five separate
     bugs. Fixed by splitting into `tsconfig.main.json` (Node16, for
     main/preload - real `require()` semantics) and `tsconfig.renderer.json`
     (`module: ES2022`, which for a file with zero imports/exports emits a
     plain script with no wrapper at all) - `package.json`'s `build` script
     now runs both.
  2. Real race, independent of bug 1: `startLogTailing()` pushed the entire
     history replay over IPC synchronously right after `win.loadFile()` -
     Electron's `ipcRenderer.on()` doesn't queue events sent before a
     listener registers, so hundreds of `log-event` sends were dropped on
     the floor before `renderer.ts`'s script had even loaded. The
     model-status badge happened to work anyway, coincidentally, because its
     first update only arrives after a real network round-trip to Ollama -
     by then the renderer had caught up. Fixed by deferring all IPC-pushing
     work (`startLogTailing`/`startTimerWatch`/`startModelPolling`, plus a
     new unconditional initial `sendLatencyUpdate()` so the latency panel
     doesn't sit empty until the next live event) until `did-finish-load`,
     which only fires once the page's own `<script>` tags have finished
     executing.
  **Verification method, real and specific to this environment's
  constraints**: OS-level screenshotting (PowerShell `System.Drawing` +
  `GetWindowRect`) gave contradictory, DPI-scale-dependent results across
  repeated calls on this machine (Electron renders per-monitor-DPI-aware;
  the calling PowerShell process isn't, and `SetProcessDPIAware()` didn't
  reliably fix it across separate tool invocations) - chased that dead end
  once, then switched to `webContents.capturePage()` from *inside* the
  Electron process itself, which sidesteps DPI entirely since it captures
  Chromium's own compositor output. Interactions (tab switches, memory
  edit/save, delete) were driven the same way, via `executeJavaScript()`
  clicking real DOM elements and typing into the real textarea, rather than
  simulated OS-level mouse events - genuinely exercises the same code path a
  real click would, not a proxy for it. `window.confirm()` was monkeypatched
  to `() => true` before the delete click specifically because a real native
  confirm dialog blocks renderer JS with nothing `executeJavaScript()` can
  click to dismiss it. All eight checkpoints (conversation, tools/activity
  with real tool-call + daemon events + a pending-timer chip, latency cards
  with real injected values, memory sessions/entries/edit-open/after-edit/
  after-delete) confirmed visually correct, and the memory edit/delete
  result was additionally confirmed against a fresh, independent
  `scripts/memory.py list --json` call - not just the UI's own view of its
  own write. All temporary diagnostic/capture code removed before commit;
  none of it shipped.
  Tools/Activity tab also folds in `daemon.jsonl` (announced/suppressed
  decisions, distinct purple-tinted rows) and a pending-timers strip read
  from `daemon_store/timers.json` - not in PROMPTS.md's literal A12 bullet
  list, but explicitly named as a source to surface in this session's
  instruction. Latency panel shows both the corrected derived total (with
  its own missing-data state, honestly labeled, when a stage has zero
  records in the current `--since`-scoped window) and a live raw-event feed
  of individual stage measurements underneath - the derived number is never
  computed client-side, only ever passed through from `compute_report()`.
  Known limitation carried over from v1, unchanged: the Tools tab is only
  live when `services/brain/agent.py`'s tool-use loop actually runs -
  standing A8/A9/A10 deferral, stated in the panel itself via a visible
  caption, not hidden.

  **Follow-up in the same session**: two explicit requests, both landed.
  (1) A second, independent opacity lever - `[ui].window_opacity`, the whole
  `BrowserWindow`'s own compositor-level `opacity` option, distinct from
  `panel_opacity`'s CSS-only background fade (text/borders stay crisp under
  `panel_opacity`; `window_opacity` fades literally everything, including
  text, since it's an OS/Windows-compositor multiply Chromium's own render
  never sees). Caught a real verification-method gap proving this out:
  `webContents.capturePage()` (this session's go-to since OS screenshotting
  had the DPI problems described above) captures Chromium's *internal*
  render, which is blind to window-level compositor opacity by construction
  - two capturePage() shots at `window_opacity` 1.0 and 0.5 came back
  pixel-identical. Had to fall back to real OS screen capture
  (`SetProcessDPIAware()` + `GetWindowRect`/`CopyFromScreen`, the same
  method abandoned earlier for DPI unreliability - reliable *within one
  script invocation*, just not trustworthy across separately-invoked
  PowerShell calls) for this one specific check, which confirmed it clearly:
  at 0.5 the whole window - editor/terminal text behind it bleeding straight
  through previously-opaque panel text - visibly blended with what was
  behind it, distinct from `panel_opacity`'s panel-only fade. Reverted to
  the correct default (1.0) after confirming.
  (2) Real persistent audit trail for memory deletions - `delete`/edit/
  confirmation already existed from the same session's earlier A12 pass, so
  the actual gap was that a deletion left no durable record of what was
  removed. `store.delete_passage()` (the one real chokepoint every deletion
  path goes through - CLI and, by extension, `ui/`'s memory tab, which
  shells out to that same CLI) now fetches the full row before deleting it
  and logs it to `logs/memory.jsonl` (the same file `embeddings.py` already
  writes `embed` records to, distinguished by `stage`) - not scattered at
  the CLI call site, so any future caller gets the same guarantee
  automatically. Caught a real bug in the fix itself, on first attempt: the
  audit record's dict literal was `{"timestamp": datetime.now(...), "stage":
  "delete", **asdict(passage)}` - but `Passage` already has its own
  `timestamp` field (when the passage was *originally written*), and dict-
  literal spread ordering let it silently clobber the deletion-moment
  timestamp, so every logged record showed the creation time twice instead
  of when the deletion actually happened. Renamed the outer key to
  `deleted_at` and re-verified against a real store: inserted a passage,
  waited 2 real seconds, deleted it, and confirmed the log's `deleted_at`
  and the passage's own `timestamp` were genuinely ~2s apart, not identical.

**Next**: A13 - the CAD data pipeline, per PROMPTS.md's sequencing now that
A12 is done. A5b
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
