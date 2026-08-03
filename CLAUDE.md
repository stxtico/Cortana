# CORTANA

Local voice assistant + agent. Companion and work tool. Read `PLAN.md` for full context
and rationale — this file is the operating summary.

## Current state

**Phase:** 0 — Environment
**Hardware:** RTX 3080 Ti (12GB VRAM), i9-12900K, 32GB RAM, dual 1440p, Windows 11
**Model:** Gemma 4 12B Unified (Q4, ~7.5GB, multimodal — covers vision too), Ollama tag
`gemma4:12b`

> Update this block every session. It's the first thing to read and the thing most likely
> to be stale.

**Done:** A0 — repo skeleton, uv project, `config/cortana.toml`, `scripts/bench.py`.
Baseline on `gemma4:12b`: warm TTFT ~4.1-4.2s, ~55-58 tok/s, flat across 1K/8K/32K context
(first run per depth is inflated by Ollama reloading on `num_ctx` change — ignore those).
Saved to `logs/bench-2026-08-03.json`.
**Next:** A1 — streaming LLM client (`services/brain/client.py`)

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
