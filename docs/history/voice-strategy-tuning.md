# Voice strategy tuning — VAD/VRAM/backchannel infra, XTTS streaming strategies (pre-A4)

Moved verbatim from CLAUDE.md's Done log (2026-08-12 restructure) — see
CLAUDE.md's Done log for the one-line pointer back to this file.

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

