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
- A2 — `services/ears/{wake,vad,stt}.py` + `pipeline.py`. openWakeWord (stand-in
  `hey_jarvis` model — no "hey cortana" model trained yet, needs voice samples across
  rooms), silero-vad (`VADIterator`, endpoint-only, real decision latency read off
  `current_sample`/`temp_end`, not total utterance duration), faster-whisper
  `large-v3-turbo` on GPU (needed `nvidia-cublas-cu12`/`nvidia-cudnn-cu12` pip packages +
  their DLL dirs added to `PATH` — ctranslate2 doesn't find CUDA otherwise on Windows).
  One shared 512-sample/32ms frame size feeds both wake and VAD. Verified end-to-end by
  feeding a synthesized "Hey Jarvis, what time is it" WAV frame-by-frame through the real
  objects (no live mic available in the build environment) — wake fired, VAD endpointed at
  322ms, STT returned the exact question. Found and fixed a real bug this way: openWakeWord's
  internal embedding window (~1-2s) still holds the wake phrase right after detection, so
  short utterances spuriously re-triggered the instant the state machine returned to
  listening — fixed with a `debounce_s` (2.0) in `[audio.wake]`. Live mic test still
  outstanding — do this before trusting the wake/VAD tuning.

**Next:** A3 — Voice: streaming TTS (`services/voice/tts.py`)

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
7. **One persistent HTTP client per process, explicit close.** Any service making repeated
   calls (Ollama, Letta, APIs) opens its client once at the process/module level and reuses
   it for the process lifetime, with an explicit `close()`/`aclose()`. A fresh client per
   call costs real connection-setup latency — cost this out on `services/brain/client.py`:
   ~280ms per call, a quarter of the first-token budget, from an `httpx.AsyncClient`
   recreated on every `stream()` call instead of reused.

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
