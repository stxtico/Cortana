# CORTANA

Local voice assistant + agent. Companion and work tool. Read `PLAN.md` for full context
and rationale — this file is the operating summary.

## Current state

**Phase:** 1 — Voice loop
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

**Next**: A5 continues - re-run `latency_report.py` after a real live session to
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
