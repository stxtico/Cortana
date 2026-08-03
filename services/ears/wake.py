"""openWakeWord wake-word detection.

No trained "hey cortana" model exists yet (needs voice samples across rooms - see
PLAN.md). `model` in config points at a bundled pretrained model as a stand-in
("hey_jarvis" by default); swap in a custom .onnx path once one is trained.
"""

import os
import time
from dataclasses import dataclass

import numpy as np
import openwakeword
from openwakeword.model import Model

_BUNDLED_MODELS_DIR = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")


@dataclass
class WakeEvent:
    score: float
    latency_ms: float


def _resolve_model_path(name: str) -> str:
    bundled = os.path.join(_BUNDLED_MODELS_DIR, f"{name}_v0.1.onnx")
    if os.path.exists(bundled):
        return bundled
    if os.path.exists(name):
        return name
    raise FileNotFoundError(f"No openWakeWord model found for '{name}' (checked bundled models dir and as a direct path)")


class WakeWordDetector:
    """Feed one frame at a time via process_frame() - never blocks. Each call is a
    single ONNX inference over one frame; openWakeWord buffers internally across
    calls so any frame size works, it just takes a few frames before the first
    score appears."""

    def __init__(self, model_name: str, threshold: float):
        model_path = _resolve_model_path(model_name)
        self._model = Model(wakeword_model_paths=[model_path])
        self._key = os.path.basename(model_path)[: -len(".onnx")]
        self.threshold = threshold

    def process_frame(self, frame: np.ndarray) -> WakeEvent | None:
        """frame: mono int16 PCM at the configured sample rate."""
        start = time.perf_counter()
        score = float(self._model.predict(frame)[self._key])
        latency_ms = (time.perf_counter() - start) * 1000
        if score >= self.threshold:
            return WakeEvent(score=score, latency_ms=latency_ms)
        return None
