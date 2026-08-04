"""Kokoro-82M: fast, predictable TTS used as the development baseline while the
streaming plumbing gets built (PROMPTS.md A3). Coqui XTTS v2 is the production
engine for the cloned voice - see xtts_engine.py, added in A3 step 3.
"""

import numpy as np
from kokoro import KPipeline

from services.voice.engine import TTSEngine


class KokoroEngine(TTSEngine):
    sample_rate = 24000

    def __init__(self, lang_code: str = "a", voice: str = "af_heart", speed: float = 1.0):
        # KPipeline load is ~1-7s (model fetch/warm) - created once here, not per call.
        self._pipeline = KPipeline(lang_code=lang_code)
        self._voice = voice
        self._speed = speed

    def synthesize(self, text: str) -> np.ndarray:
        # Kokoro's own generator can still split one sentence into multiple chunks
        # (e.g. if it's unusually long) - concatenate defensively rather than assume one.
        segments = [audio for _, _, audio in self._pipeline(text, voice=self._voice, speed=self._speed)]
        if not segments:
            return np.zeros(0, dtype=np.float32)
        arrays = [s.numpy() if hasattr(s, "numpy") else np.asarray(s) for s in segments]
        return np.concatenate(arrays).astype(np.float32)
