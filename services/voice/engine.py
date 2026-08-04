"""Interface every TTS engine implements. tts.py's speak_stream() and sanitize()
never reference an engine's internals directly - only this interface - so switching
[voice].engine in cortana.toml (Kokoro -> XTTS, PROMPTS.md A3 step 3) touches config
only, not the calling path.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import numpy as np


class TTSEngine(ABC):
    sample_rate: int

    @abstractmethod
    def synthesize(self, text: str) -> np.ndarray:
        """Blocking. text is one already-sanitized sentence. Returns mono float32
        PCM at self.sample_rate. Call via asyncio.to_thread from async code."""
        raise NotImplementedError

    def synthesize_stream(self, text: str) -> AsyncIterator[np.ndarray]:
        """Optional. Yields mono float32 PCM chunks progressively as they're
        generated, conditioning on the *entire* text at once (not per-chunk
        isolated text, unlike splitting text across multiple synthesize() calls) -
        see xtts_engine.py's implementation for why that distinction matters
        (CLAUDE.md's path-divergence investigation).

        Default: not supported. A plain (non-async, non-generator) method so calling
        it raises NotImplementedError immediately, before any iteration - tts.py's
        "inference_stream" strategy checks whether this has been overridden
        (type(engine).synthesize_stream is not TTSEngine.synthesize_stream) and
        falls back to per_sentence rather than ever triggering this raise in
        practice. Kept as a real raise anyway for any direct caller."""
        raise NotImplementedError

    def close(self) -> None:
        """Release engine resources (e.g. GPU memory). Default no-op - override if
        the engine holds something worth freeing explicitly."""
        pass
