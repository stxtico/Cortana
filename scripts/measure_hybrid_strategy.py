"""Measures the hybrid strategy's real cost: time-to-first-audio, and the gap
between sentence 1's playback ending and the remainder's playback starting, on a
realistic multi-sentence response with realistic token pacing. Token pace matches
gemma4:e4b's actual measured speed (~104 tok/s average from the A0 bench rerun,
~1.3 tokens/word -> ~80 words/sec), not an arbitrary rate.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.voice import tts

RESPONSE = (
    "Got it. "
    "Your meeting's at three, and the CAD job finished about an hour ago. "
    "The export looks clean, no errors in the log, and the STEP file is ready for review. "
    "I'll flag it if anything changes before then."
)

WORDS_PER_SEC = 80  # gemma4:e4b measured ~104 tok/s average, ~1.3 tokens/word
DELAY_S = 1 / WORDS_PER_SEC

T0 = None


def log(msg):
    print(f"[t={time.perf_counter() - T0:5.2f}s] {msg}", flush=True)


async def realistic_tokens():
    words = RESPONSE.split(" ")
    for i, word in enumerate(words):
        suffix = "" if i == len(words) - 1 else " "
        yield word + suffix
        await asyncio.sleep(DELAY_S)


async def main():
    global T0
    T0 = time.perf_counter()
    log("warming engine (discarded)...")
    await tts.speak("Warmup.")
    log("engine warm")

    T0 = time.perf_counter()
    log(f"speak_stream start (strategy=hybrid3) - {len(RESPONSE.split())} words at ~{WORDS_PER_SEC}/s")
    await tts.speak_stream(realistic_tokens(), strategy="hybrid3")
    log("speak_stream done")


asyncio.run(main())
