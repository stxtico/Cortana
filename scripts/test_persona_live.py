"""Diagnostic only. Runs several real questions through the real live persona -
brain_client.stream() with config/persona.md as the system prompt, straight into
speak_stream() - so the audio actually plays on this machine's speakers, same as
a real conversational turn. Not a test of correctness, a listening check.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


async def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "correction":
        await run_correction()
    elif arg is not None:
        await run_one(QUESTIONS[int(arg)])
    else:
        for q in QUESTIONS:
            await run_one(q)
        await run_correction()
    await brain_client.aclose()
    tts.close()


if __name__ == "__main__":
    asyncio.run(main())
