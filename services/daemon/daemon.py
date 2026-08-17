"""daemon - PROMPTS.md A11's orchestrator. Every other module in this package
already existed before this file (timers.py is the one real, working trigger;
calendar_trigger.py/email_trigger.py are dormant, self-gated exactly like A8's
web_search and A9's shell, until Outlook is running; relevance.py is the
interruption filter; output.py is the swappable output path) - this is what
actually ties poll -> relevance -> quiet hours -> rate limit -> announce into
a running loop. Nothing polled any of those modules before this file existed.

Coexistence with services/brain/loop.py's conversation process (decided
before writing this, not incidentally, per the A11 prompt's explicit
instruction to state the approach first):
  - Both processes are plain HTTP clients to the same already-running Ollama
    server (services/brain/client.py's module-level httpx client) - no second
    model load, same reasoning [models] already relies on for the
    conversation loop itself.
  - The one resource that genuinely can't be duplicated safely is voice
    output: a second XTTS checkpoint loaded onto the GPU by a second process
    is a real, measured VRAM cost this project doesn't have headroom for (see
    CLAUDE.md's VRAM investigation). This daemon deliberately never imports
    services.voice.tts - only services.voice.playback_state, the
    dependency-free cross-process flag file that module's own docstring
    designed for exactly this (see its "Import this module on its own, never
    services.voice.tts from services/daemon/" instruction).
  - Self-interruption is prevented by waiting, not by racing: before
    announcing, this daemon polls playback_state.is_active() and holds off
    until it goes false, rather than firing regardless and hoping nothing was
    mid-response. It never cancels or talks over a real response in
    progress - it just waits for the next gap between turns.

Run standalone: `python -m services.daemon.daemon`.
"""

import asyncio
import json
import time
import tomllib
from datetime import datetime, time as dtime, timezone
from pathlib import Path

from services.daemon import calendar_trigger, email_trigger, greeting_signal, output, relevance, session_trigger, timers, worker_trigger
from services.voice import playback_state

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"
LOG_PATH = ROOT / "logs" / "daemon.jsonl"

# Every trigger source polled on the general [daemon].poll_interval_s
# cadence (30s default) - timers first since it's the only one that's
# actually live on this machine, order otherwise doesn't affect behavior,
# just log readability. worker_trigger (PROMPTS.md A21) is the mechanism
# behind "she tells you when it's done" - a delegated task's completion
# becomes a candidate the same way a fired timer already does.
#
# session_trigger (session-start greeting) is deliberately NOT in this list
# - it's polled on its own faster cadence by _session_loop() below (see that
# function's docstring for why 30s is too slow for this one source), and its
# candidates take a different path through _handle_candidate (see
# _SESSION_SOURCE_NAME) since a greeting isn't an interruption and the
# daemon has no speech path of its own to deliver it with regardless. Still
# "another trigger, not a new system" - same module shape, same dedup set,
# same _handle_candidate entry point - just a different poll cadence and
# delivery mechanism, both for real, documented reasons.
_SOURCES = [timers, worker_trigger, calendar_trigger, email_trigger]
_SESSION_SOURCE_NAME = "session"


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("daemon", {})


def _think_default() -> bool:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("thinking", {}).get("daemon", False)


def _log(record: dict) -> None:
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}) + "\n")


def _parse_hhmm(value: str) -> dtime:
    hour, minute = value.split(":")
    return dtime(int(hour), int(minute))


def _in_quiet_hours(config: dict, now: datetime | None = None) -> bool:
    start = _parse_hhmm(config.get("quiet_hours_start", "22:00"))
    end = _parse_hhmm(config.get("quiet_hours_end", "08:00"))
    current = (now or datetime.now()).time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end  # window wraps past midnight


class RateLimiter:
    """In-memory rolling-hour count of announcements actually made -
    resets on daemon restart. Accepted, not fixed: the default limit (2/hour,
    CLAUDE.md's explicit "default low" instruction) is already conservative
    enough that a restart-triggered reset isn't a real way to dodge it in
    normal operation, and persisting this to disk would be a second piece of
    state (daemon_store/rate_limit.json or similar) for a problem that isn't
    actually observed."""

    def __init__(self, max_per_hour: int) -> None:
        self.max_per_hour = max_per_hour
        self._timestamps: list[float] = []

    def has_capacity(self) -> bool:
        cutoff = time.time() - 3600.0
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        return len(self._timestamps) < self.max_per_hour

    def record(self) -> None:
        self._timestamps.append(time.time())


async def _poll_all() -> list[dict]:
    candidates: list[dict] = []
    for source in _SOURCES:
        try:
            if not await source.is_available():
                continue
            candidates.extend(await source.poll())
        except Exception as exc:
            _log({"stage": "poll_error", "source": source.__name__, "error": str(exc)})
    return candidates


async def _wait_for_playback(max_wait_s: float) -> bool:
    """Holds off announcing while a real response is playing. Bounded, not an
    unconditional wait: playback_state.json could in principle get stuck
    "active" (e.g. a crash between mark_started() and mark_stopped()) - a
    bounded wait means a stuck flag delays one announcement instead of
    silencing the daemon forever. Returns whether it's now safe to speak."""
    deadline = time.time() + max_wait_s
    while playback_state.is_active():
        if time.time() >= deadline:
            return False
        await asyncio.sleep(1.0)
    return True


async def _handle_candidate(
    candidate: dict, config: dict, limiter: RateLimiter, think: bool, max_wait_s: float, announced_ids: set[str]
) -> None:
    # Session-start greeting: an explicit, logged bypass of quiet hours, the
    # rate limiter, AND the relevance filter - none of the three protections
    # this function exists to apply are answering the question they were
    # built for here. Quiet hours and rate limiting exist to stop the daemon
    # interrupting the user unprompted; a greeting only ever fires because
    # the user just launched the app themselves, which is the opposite of
    # unprompted. Relevance answers "is this worth interrupting for" - a
    # greeting isn't an interruption at all, there's nothing else going on
    # to interrupt. Delivered via greeting_signal's file handoff, not
    # output.announce() (print+toast) - the daemon has no speech path of its
    # own (see this module's docstring), so services/brain/loop.py is the
    # one that actually speaks it.
    if candidate["source"] == _SESSION_SOURCE_NAME:
        if not await _wait_for_playback(max_wait_s):
            _log({"stage": "suppressed", "reason": "playback_wait_timeout", **candidate})
            return
        greeting_signal.write_greeting(candidate["summary"], candidate["id"].removeprefix("session-"))
        # Seed the worker-completion ids this greeting already covered into
        # the shared dedup set, so worker_trigger's own next poll doesn't
        # separately re-announce the same finished tasks a moment later -
        # the double-announcement risk flagged when this design was decided.
        announced_ids.update(candidate.get("mentioned_worker_ids", []))
        _log({"stage": "announced", "source": candidate["source"], "id": candidate["id"], "summary": candidate["summary"]})
        return

    # Cheap gates first - no reason to spend an LLM call on something quiet
    # hours or the rate limit would suppress anyway.
    if _in_quiet_hours(config):
        _log({"stage": "suppressed", "reason": "quiet_hours", **candidate})
        return
    if not limiter.has_capacity():
        _log({"stage": "suppressed", "reason": "rate_limited", **candidate})
        return

    relevant = await relevance.is_relevant(candidate, think=think)
    if not relevant:
        _log({"stage": "suppressed", "reason": "not_relevant", **candidate})
        return

    if not await _wait_for_playback(max_wait_s):
        _log({"stage": "suppressed", "reason": "playback_wait_timeout", **candidate})
        return

    limiter.record()
    _log({"stage": "announced", **candidate})
    await output.announce(candidate["summary"])


async def _tick(config: dict, limiter: RateLimiter, announced_ids: set[str], think: bool, max_wait_s: float) -> None:
    candidates = await _poll_all()
    fresh = [c for c in candidates if c["id"] not in announced_ids]
    for c in fresh:
        # Marked seen the instant it's found, whether or not it ends up
        # announced (suppressed-but-seen still shouldn't re-evaluate every
        # poll cycle) - same "the event itself is the mark" reasoning
        # timers.py already applies to its own store.
        announced_ids.add(c["id"])
        await _handle_candidate(c, config, limiter, think, max_wait_s, announced_ids)


async def _session_loop(config: dict, limiter: RateLimiter, announced_ids: set[str], max_wait_s: float, interval: float) -> None:
    """session_trigger polled on its own faster cadence, decoupled from
    _tick()'s general [daemon].poll_interval_s (30s default). Found live
    while wiring this up, not assumed: a session-start greeting has to be
    detected in a handful of seconds (services/brain/loop.py only waits a
    bounded amount at its own startup before giving up and starting
    silently - see that module), and the other sources' 30s cadence is
    tuned for things that genuinely don't need faster checking (an unread
    email, a calendar event 20 minutes out). Sharing that cadence would
    mean the greeting is very often composed too late to ever reach
    loop.py's own wait window. This loop's own poll is still cheap in the
    common case - session_trigger.poll() only pays the real LLM-composition
    cost on an actual session_id change, a plain file read otherwise (see
    that module's own _last_composed_session_id gate)."""
    while True:
        try:
            for candidate in await session_trigger.poll():
                if candidate["id"] in announced_ids:
                    continue
                announced_ids.add(candidate["id"])
                await _handle_candidate(candidate, config, limiter, False, max_wait_s, announced_ids)
        except Exception as exc:
            _log({"stage": "poll_error", "source": session_trigger.__name__, "error": str(exc)})
        await asyncio.sleep(interval)


async def run() -> None:
    config = _load_config()
    interval = config.get("poll_interval_s", 30)
    max_wait_s = config.get("max_playback_wait_s", 60)
    greeting_interval = config.get("greeting", {}).get("poll_interval_s", 2)
    limiter = RateLimiter(config.get("rate_limit_per_hour", 2))
    announced_ids: set[str] = set()
    think = _think_default()

    _log({
        "stage": "daemon_start",
        "poll_interval_s": interval,
        "greeting_poll_interval_s": greeting_interval,
        "rate_limit_per_hour": limiter.max_per_hour,
        "quiet_hours": f"{config.get('quiet_hours_start')}-{config.get('quiet_hours_end')}",
    })

    async def _main_loop() -> None:
        while True:
            await _tick(config, limiter, announced_ids, think, max_wait_s)
            await asyncio.sleep(interval)

    try:
        await asyncio.gather(
            _main_loop(),
            _session_loop(config, limiter, announced_ids, max_wait_s, greeting_interval),
        )
    except asyncio.CancelledError:
        _log({"stage": "daemon_stop"})
        raise


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
