"""Closes the loop (PROMPTS.md A4): wake word -> transcribe -> completeness check
-> stream to LLM -> stream to TTS, running continuously as one conversation.

Deliberately thin - every stage already exists and does its own job well, this
module just wires them together:
- services/ears/pipeline.py's listen() owns the mic, and already does wake ->
  verify -> VAD -> STT -> completeness -> backchannel/resume -> yield. This module
  only ever consumes its yielded utterances, never reimplements any of that.
- config/persona.md is loaded verbatim as the system prompt.
- services/brain/client.py's stream() is the LLM call, unchanged.
- services/voice/tts.py's speak_stream() already picks its synthesis strategy from
  [voice].strategy - this module just feeds it a token stream.

Barge-in: pipeline.listen()'s on_wake callback (services/ears/pipeline.py) fires
the instant a debounced wake event is detected, before verification/recording even
start - the earliest signal available for "the user wants attention now." _on_wake
below cancels whatever response task is in flight; cancellation propagates through
speak_stream() into _play_all()/play_audio(), where stream.abort()/sd.stop() were
already verified (A3) to halt playback promptly. Nothing new needed at the
playback layer - only the trigger, wired here.

_on_wake fires on *every* wake trigger, including the one that starts the very
turn whose response_task it might cancel - it has no way to tell "genuine
interruption" from "the wake that's about to yield a brand new utterance" apart
from response_task's own state. First live test found this cancelling turns that
had never played any audio yet (still mid-LLM-stream), silently killing them
before they could produce anything - see [brain.barge_in].min_playback_s in
cortana.toml: only cancel if tts.response_playback_elapsed_s() shows real,
ongoing audio that's been playing at least that long. Every decision (cancelled
or skipped, and why) is logged to logs/loop.jsonl so this can't go quiet again.
"""

import asyncio
import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from services.brain import client as brain_client
from services.ears import pipeline
from services.voice import tts as voice_tts

ROOT = Path(__file__).resolve().parent.parent.parent
PERSONA_PATH = ROOT / "config" / "persona.md"
CONFIG_PATH = ROOT / "config" / "cortana.toml"
LOOP_LOG_PATH = ROOT / "logs" / "loop.jsonl"


def _load_persona() -> str:
    return PERSONA_PATH.read_text(encoding="utf-8") if PERSONA_PATH.exists() else ""


def _load_barge_in_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("brain", {}).get("barge_in", {})


def _log(record: dict) -> None:
    LOOP_LOG_PATH.parent.mkdir(exist_ok=True)
    with LOOP_LOG_PATH.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}) + "\n")


async def _respond(user_text: str, history: list[dict]) -> None:
    """Streams one conversational turn: brain tokens straight into speak_stream()
    (rule 1 - never wait for the full response), history updated either way. The
    user's line is recorded immediately so a later turn still has it even if this
    response gets interrupted; the assistant's line is whatever was actually
    generated before that happened, partial or complete - that's what really
    happened in the conversation, not nothing."""
    history.append({"role": "user", "content": user_text})
    messages = list(history)
    assistant_chunks: list[str] = []

    async def _tokens():
        async for token in brain_client.stream(messages, think=False):
            assistant_chunks.append(token)
            yield token

    try:
        await voice_tts.speak_stream(_tokens())
    finally:
        if assistant_chunks:
            history.append({"role": "assistant", "content": "".join(assistant_chunks)})


def _on_response_task_done(task: "asyncio.Task[None]") -> None:
    """Attached to every response_task the instant it's created. A task's
    exception is otherwise only ever surfaced if something later awaits it -
    miss that (loop exits, next iteration never comes, process gets Ctrl+C'd
    first) and asyncio just logs "exception was never retrieved" at garbage
    collection, easy to miss entirely. This is exactly how utterance 2 could
    burn 24 seconds and leave nothing in brain.jsonl: whatever happened inside
    _respond() never got surfaced anywhere. Every completion path is logged here
    unconditionally, independent of whether run()'s loop ever awaits this task
    itself."""
    if task.cancelled():
        _log({"stage": "response_task_done", "outcome": "cancelled"})
        return
    exc = task.exception()
    if exc is not None:
        _log({"stage": "response_task_done", "outcome": "error", "error": repr(exc)})
        print(f"(response task failed: {exc!r})")
    else:
        _log({"stage": "response_task_done", "outcome": "ok"})


async def run() -> None:
    persona = _load_persona()
    history: list[dict] = [{"role": "system", "content": persona}] if persona else []
    response_task: asyncio.Task | None = None
    min_playback_s = _load_barge_in_config().get("min_playback_s", 0.3)

    def _on_wake() -> None:
        if response_task is None or response_task.done():
            _log({"stage": "wake_cancel", "action": "skipped", "reason": "no_active_response"})
            return
        elapsed = voice_tts.response_playback_elapsed_s()
        if elapsed is None or elapsed < min_playback_s:
            _log({
                "stage": "wake_cancel", "action": "skipped", "reason": "not_playing_yet",
                "elapsed_s": round(elapsed, 3) if elapsed is not None else None,
            })
            return
        _log({"stage": "wake_cancel", "action": "cancelled", "reason": "barge_in", "elapsed_s": round(elapsed, 3)})
        response_task.cancel()

    print("Cortana is listening... (Ctrl+C to stop)\n")
    async for user_text in pipeline.listen(on_wake=_on_wake):
        if response_task is not None and not response_task.done():
            response_task.cancel()
        if response_task is not None:
            try:
                await response_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                print(f"(previous response errored: {exc!r})")

        print(f"> {user_text}")
        response_task = asyncio.ensure_future(_respond(user_text, history))
        response_task.add_done_callback(_on_response_task_done)

    if response_task is not None:
        try:
            await response_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"(final response errored: {exc!r})")


async def _main() -> None:
    try:
        await run()
    finally:
        voice_tts.close()
        await brain_client.aclose()


if __name__ == "__main__":
    import faulthandler
    faulthandler.enable()
    asyncio.run(_main())
