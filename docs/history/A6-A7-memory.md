# A6/A7 — Memory (hand-rolled, not Letta)

Moved verbatim from CLAUDE.md's Done log (2026-08-12 restructure) — see
CLAUDE.md's Done log for the one-line pointer back to this file.

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

