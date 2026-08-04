"""Decides what happens to a just-transcribed, VAD-endpointed utterance: pass it on
as-is, or treat it as abandoned mid-thought and buy time instead of guessing.

Flow per utterance (see BackchannelSession.handle_utterance):
1. Combine with any pending fragment from an earlier round (a resume continues the
   same thought, it doesn't start a new one).
2. Run the non-LLM completeness heuristic (services/ears/completeness.py) on the
   combined text.
3. Complete -> yield it, clear pending state, reset the rate-limit escalation.
4. Abandoned -> don't pass it to the brain. If not currently rate-limited, pop a
   pre-rendered line from the pool (services/ears/backchannel_pool.py) and signal the
   caller to play it; either way, signal the caller to listen for a resume for
   resume_window_s without requiring the wake word again - a person continuing a
   thought doesn't re-say the assistant's name.
5. If the resume window elapses with nothing said, handle_resume_timeout() finalizes:
   yields whatever fragment exists anyway (silence eventually does mean done, and
   swallowing what was said is worse than passing on a possibly-incomplete fragment),
   and escalates the rate limit so the next abandoned-detection waits longer before
   trying another backchannel.

The completeness prediction is consumed entirely inside this module. Nothing derived
from it - not the verdict, not a guess at the finished thought - ever becomes spoken
output; only pre-written pool lines get played.
"""

import time
from dataclasses import dataclass

import numpy as np

from services.ears.backchannel_pool import PoolEntry, get_pool
from services.ears.completeness import check_completeness


@dataclass
class Decision:
    action: str  # "yield" | "backchannel" | "wait_silently"
    text: str | None = None                        # set when action == "yield"
    backchannel: PoolEntry | None = None            # set when action == "backchannel"
    resume_window_s: float = 0.0                    # set when action in (backchannel, wait_silently)


class BackchannelSession:
    """One instance per live pipeline run - holds the pending-fragment and
    rate-limit state across utterances. A fresh instance per services.ears.pipeline
    listen() call, not a module-level singleton, so independent runs (tests, or a
    future multi-session setup) don't share state."""

    def __init__(
        self, resume_window_s: float = 4.0, base_cooldown_s: float = 20.0,
        escalation_factor: float = 3.0, max_cooldown_s: float = 300.0,
    ):
        self.resume_window_s = resume_window_s
        self.base_cooldown_s = base_cooldown_s
        self.escalation_factor = escalation_factor
        self.max_cooldown_s = max_cooldown_s

        self._pending_text: str | None = None
        self._cooldown_until = 0.0
        self._consecutive_unanswered = 0

    def _next_cooldown_s(self) -> float:
        return min(self.max_cooldown_s, self.base_cooldown_s * (self.escalation_factor ** self._consecutive_unanswered))

    def handle_utterance(self, text: str, audio: np.ndarray, sample_rate: int) -> Decision:
        combined = f"{self._pending_text} {text}".strip() if self._pending_text else text

        result = check_completeness(text, audio, sample_rate)
        if result.complete:
            self._pending_text = None
            self._consecutive_unanswered = 0  # a real resolution - the rate limit isn't punishing normal turns
            return Decision(action="yield", text=combined)

        self._pending_text = combined

        if time.monotonic() < self._cooldown_until:
            return Decision(action="wait_silently", resume_window_s=self.resume_window_s)

        entry = get_pool().take()
        if entry is None:
            # Pre-rendering is non-negotiable - an empty pool means no backchannel
            # this time, not a live-generated fallback. Still wait for a resume.
            return Decision(action="wait_silently", resume_window_s=self.resume_window_s)

        self._cooldown_until = time.monotonic() + self.base_cooldown_s
        return Decision(action="backchannel", backchannel=entry, resume_window_s=self.resume_window_s)

    def handle_resume_timeout(self) -> str | None:
        """Call when resume_window_s elapses with no new speech at all. Returns the
        pending fragment to finalize and yield (None if there wasn't one - shouldn't
        happen if the caller only calls this after a backchannel/wait_silently
        decision, but safe either way)."""
        pending = self._pending_text
        self._pending_text = None
        if pending is not None:
            self._consecutive_unanswered += 1
            self._cooldown_until = time.monotonic() + self._next_cooldown_s()
        return pending
