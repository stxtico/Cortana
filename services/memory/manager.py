"""Ties the three memory layers together (PROMPTS.md A6): profile.md injected
every turn, a rolling summary that absorbs the oldest conversation once
[models].context_window fills past [memory].rolling_fill_threshold, and vector
retrieval of everything ever said. services/brain/loop.py is the only real
caller - this module owns no I/O device, just state + the store/embeddings/
summarize modules underneath it.

One MemoryManager per process run = one session. session_id is generated at
construction and stamped on every passage written this run, which is what lets
scripts/memory.py answer "what was learned from which session."

Storage writes fire the instant a message is known (build_messages() for the
user's line, append_assistant() for the reply) rather than batched at end of
turn - a turn interrupted by barge-in (services/brain/loop.py's on_wake) can
leave an unpaired user message with no assistant reply, and storing per-message
as it happens sidesteps having to reconstruct "how many new messages this turn"
after that kind of interruption.
"""

import asyncio
import json
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from services.brain import client as brain_client
from services.memory import embeddings, profile, store, summarize

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"
MEMORY_LOG_PATH = ROOT / "logs" / "memory.jsonl"


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return {**config["memory"], "context_window": config["models"]["context_window"]}


def _log(record: dict) -> None:
    MEMORY_LOG_PATH.parent.mkdir(exist_ok=True)
    with MEMORY_LOG_PATH.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}) + "\n")


def new_session_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


@dataclass
class MemoryManager:
    session_id: str = field(default_factory=new_session_id)
    turns: list[dict] = field(default_factory=list)
    summary: str = ""
    _compressing: bool = field(default=False, init=False)
    _background_tasks: set = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self._config = _load_config()
        db_path = ROOT / self._config["db_path"]
        self._conn = store.get_connection(db_path, self._config["embedding_dim"])
        _log({"stage": "session_start", "session_id": self.session_id})

    def _spawn(self, coro, label: str) -> None:
        task = asyncio.ensure_future(coro)
        self._background_tasks.add(task)

        def _done(t: "asyncio.Task") -> None:
            self._background_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                _log({"stage": label, "outcome": "error", "error": repr(exc)})

        task.add_done_callback(_done)

    async def drain(self, timeout: float = 10.0) -> None:
        """Waits for every in-flight background task (storage writes, an
        in-progress compression) before the process shuts down. Without this,
        a session's very last message can be silently lost: build_messages()/
        append_assistant() fire storage as a background task and return
        immediately, and asyncio.run() tears down the event loop the instant
        _main() returns - an in-flight task at that point is orphaned, not
        awaited, not an error either. Found live: session 2's final reply in
        A6's end-to-end check never reached disk because the process exited
        before its store task finished."""
        if not self._background_tasks:
            return
        pending = list(self._background_tasks)
        done, still_pending = await asyncio.wait(pending, timeout=timeout)
        if still_pending:
            _log({"stage": "drain", "outcome": "timeout", "still_pending": len(still_pending)})

    def build_messages(self, persona: str, user_text: str, retrieved: list[store.Passage]) -> list[dict]:
        parts = [persona]
        user_profile = profile.load_profile().strip()
        if user_profile:
            parts.append("## What you know about the user\n" + user_profile)
        if self.summary:
            parts.append("## Earlier in this conversation (summarized)\n" + self.summary)
        if retrieved:
            fragments = "\n".join(f"- ({p.timestamp[:10]}) {p.text}" for p in retrieved)
            parts.append("## Possibly relevant, from past conversations\n" + fragments)
        system = "\n\n".join(parts)
        self.turns.append({"role": "user", "content": user_text})
        self._spawn(self._store_passage("user", user_text), "store_user")
        return [{"role": "system", "content": system}, *self.turns]

    def append_assistant(self, text: str) -> None:
        self.turns.append({"role": "assistant", "content": text})
        self._spawn(self._store_passage("assistant", text), "store_assistant")

    async def retrieve(self, query_text: str) -> list[store.Passage]:
        top_k = self._config["retrieval_top_k"]
        try:
            vector = await embeddings.embed(query_text)
            passages = await asyncio.to_thread(store.query_similar, self._conn, vector, top_k)
        except Exception as exc:
            # Retrieval is an enhancement, not a requirement - a down embedding
            # endpoint or a fresh empty store shouldn't block the turn itself.
            _log({"stage": "retrieve", "outcome": "error", "error": repr(exc)})
            return []
        _log({
            "stage": "retrieve", "outcome": "ok", "query_chars": len(query_text),
            "results": len(passages), "top_distance": passages[0].distance if passages else None,
        })
        return passages

    async def _store_passage(self, role: str, text: str, source: str = "raw") -> None:
        vector = await embeddings.embed(text)
        await asyncio.to_thread(store.write_passage, self._conn, self.session_id, role, source, text, vector)

    def spawn_after_turn(self) -> None:
        """Fire-and-forget entry point for services/brain/loop.py - never await
        this directly, that's the whole point (compression must not block the
        response path)."""
        self._spawn(self.after_turn(), "after_turn")

    async def after_turn(self) -> None:
        """Called once the response has already been spoken (services/brain/
        loop.py) - only the rolling-context compression check happens here now;
        per-message storage already fired from build_messages()/append_assistant()
        above, so this never adds latency to anything the user is waiting on."""
        await self._maybe_compress()

    async def _maybe_compress(self) -> None:
        if self._compressing:
            return
        stats = brain_client.last_call_stats()
        if not stats or stats.get("prompt_eval_count") is None:
            return
        fill = stats["prompt_eval_count"] / self._config["context_window"]
        threshold = self._config["rolling_fill_threshold"]
        chunk_n = self._config["rolling_chunk_messages"]
        min_recent = self._config["rolling_min_recent_messages"]
        if fill < threshold:
            return
        if len(self.turns) < chunk_n + min_recent:
            _log({"stage": "compress", "outcome": "skipped", "reason": "not_enough_history", "fill": round(fill, 3)})
            return

        self._compressing = True
        try:
            chunk, self.turns = self.turns[:chunk_n], self.turns[chunk_n:]
            new_summary = await summarize.summarize_chunk(self.summary, chunk)
            self.summary = new_summary
            _log({
                "stage": "compress", "outcome": "ok", "fill": round(fill, 3),
                "messages_folded": len(chunk), "summary_chars": len(new_summary),
            })
        except Exception as exc:
            # Compression failing shouldn't lose the chunk - put it back rather
            # than silently dropping it from the live context.
            self.turns = chunk + self.turns
            _log({"stage": "compress", "outcome": "error", "error": repr(exc)})
        finally:
            self._compressing = False
