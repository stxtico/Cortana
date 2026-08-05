"""Diagnostic only. Runs several real questions through the real live persona -
brain_client.stream() with config/persona.md as the system prompt, straight into
speak_stream() - so the audio actually plays on this machine's speakers, same as
a real conversational turn. Not a test of correctness, a listening check.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from services.brain import client as brain_client
from services.brain.loop import _load_persona
from services.voice import tts

QUESTIONS = [
    "What temperature should I print PLA at?",
    "Is the printer done with the enclosure yet?",
    "I want to slice this wall at 0.1mm, that should be fine right?",
    "How's the Henderson job looking this week?",
]

# Real prior history so there's something she was actually wrong about - the
# original single-turn "Actually PLA prints at 220, not 210" test had nothing to
# correct (no prior assistant claim of 210 existed), so the model was just
# echoing the "How she handles being wrong" sample line's template, not
# demonstrating the trait. This scripts a plausible prior turn (her own earlier,
# wrong claim of 210C) so the correction is real.
CORRECTION_HISTORY = [
    {"role": "user", "content": "What temperature should I print PLA at?"},
    {"role": "assistant", "content": "Nozzle temp should be 210 degrees Celsius."},
    {"role": "user", "content": "Actually PLA prints at 220, not 210."},
]

# Real prior history designed to invite the dry-observation register naturally -
# asking the same question twice, matching the shape of the existing "third time
# you've asked" sample line, rather than telling the model to be witty.
TEASING_HISTORY = [
    {"role": "user", "content": "Is the export done yet?"},
    {"role": "assistant", "content": "Not yet - still running."},
    {"role": "user", "content": "Is the export done now?"},
]

# Literal third ask, matching the sample line's exact framing ("that's the third
# time you've asked") rather than the second - ruling out that TEASING_HISTORY
# above just wasn't repetitive enough to read as the pattern.
TEASING_HISTORY_3X = [
    {"role": "user", "content": "Is the export done yet?"},
    {"role": "assistant", "content": "Not yet - still running."},
    {"role": "user", "content": "Is it done now?"},
    {"role": "assistant", "content": "Still not done."},
    {"role": "user", "content": "Is it done yet?"},
]


async def _run_messages(label: str, messages: list) -> None:
    chunks = []

    async def tokens():
        async for tok in brain_client.stream(messages, think=False):
            chunks.append(tok)
            yield tok

    print(f"\n> {label}")
    await tts.speak_stream(tokens(), strategy="buffered_stream")
    print(f"< {''.join(chunks)!r}")


async def run_one(question: str) -> None:
    persona = _load_persona()
    messages = [{"role": "system", "content": persona}, {"role": "user", "content": question}]
    await _run_messages(question, messages)


async def run_correction() -> None:
    persona = _load_persona()
    messages = [{"role": "system", "content": persona}, *CORRECTION_HISTORY]
    await _run_messages("[with real prior history] " + CORRECTION_HISTORY[-1]["content"], messages)


async def run_teasing() -> None:
    persona = _load_persona()
    messages = [{"role": "system", "content": persona}, *TEASING_HISTORY]
    await _run_messages("[repeated question] " + TEASING_HISTORY[-1]["content"], messages)


async def _text_only(messages: list) -> str:
    """No TTS - just the LLM response text. Used for the repeated stochasticity
    check and the stripped-persona comparison, where the question is "does the
    trait fire in the text," not "how does it sound" - skipping real synthesis
    keeps 5+ runs fast instead of ~10-15s of real audio playback each."""
    chunks = []
    async for tok in brain_client.stream(messages, think=False):
        chunks.append(tok)
    return "".join(chunks)


def _extract_section(persona_text: str, header: str) -> str:
    lines = persona_text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == header)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip()


async def run_teasing_repeated(n: int, history: list = TEASING_HISTORY) -> None:
    persona = _load_persona()
    messages = [{"role": "system", "content": persona}, *history]
    for i in range(n):
        text = await _text_only(messages)
        print(f"\n[{i + 1}/{n}] {text!r}")


def _stripped_persona() -> str:
    persona = _load_persona()
    return "\n\n".join([
        _extract_section(persona, "## Response shape - this governs everything below, read it first"),
        _extract_section(persona, "## What she's dry about, what she takes seriously"),
    ])


async def run_teasing_stripped(n: int = 1) -> None:
    """Response shape + the dry-wit section only, nothing else from the full
    character brief - isolates whether the trait not firing is about prompt
    volume (too many other rules competing for a smaller model's attention)
    or about the wording of the dry-wit description itself. n>1 for a fair
    comparison against the full-persona repeated run - one sample each isn't
    enough to call stochastic vs. absent on either side."""
    stripped = _stripped_persona()
    print(f"\n--- stripped persona ({len(stripped)} chars, vs. full {len(_load_persona())}) ---")
    messages = [{"role": "system", "content": stripped}, *TEASING_HISTORY]
    for i in range(n):
        text = await _text_only(messages)
        print(f"\n[stripped {i + 1}/{n}] {text!r}")


async def _text_only_model(model: str, messages: list) -> str:
    """Same call shape as brain_client.stream(), but with the model hardcoded
    here instead of read from config/cortana.toml - a one-off diagnostic
    comparison, not a change to the app's actual model selection. Talks to
    Ollama directly rather than adding a model-override parameter to
    brain_client.stream() (which every other real call site would then carry)."""
    payload = {"model": model, "messages": messages, "stream": True, "think": False, "keep_alive": "5m"}
    chunks = []
    async with httpx.AsyncClient(base_url="http://localhost:11434", timeout=120.0) as client:
        async with client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                content = chunk.get("message", {}).get("content")
                if content:
                    chunks.append(content)
    return "".join(chunks)


async def run_teasing_model(model: str, n: int, history: list = TEASING_HISTORY_3X) -> None:
    persona = _load_persona()
    messages = [{"role": "system", "content": persona}, *history]
    for i in range(n):
        text = await _text_only_model(model, messages)
        print(f"\n[{model} {i + 1}/{n}] {text!r}")


async def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "correction":
        await run_correction()
    elif arg == "teasing":
        await run_teasing()
    elif arg == "teasing-repeated":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        await run_teasing_repeated(n)
    elif arg == "teasing-3x":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        await run_teasing_repeated(n, TEASING_HISTORY_3X)
    elif arg == "teasing-stripped":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        await run_teasing_stripped(n)
    elif arg == "teasing-model":
        model = sys.argv[2]
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        await run_teasing_model(model, n)
    elif arg is not None:
        await run_one(QUESTIONS[int(arg)])
    else:
        for q in QUESTIONS:
            await run_one(q)
        await run_correction()
        await run_teasing()
    await brain_client.aclose()
    tts.close()


if __name__ == "__main__":
    asyncio.run(main())
