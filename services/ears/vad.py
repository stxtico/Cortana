"""silero-vad endpoint detection. An utterance ends when the model confirms trailing
silence, never after a fixed recording duration.
"""

from dataclasses import dataclass

import numpy as np
from silero_vad import VADIterator, load_silero_vad


@dataclass
class VadEvent:
    kind: str          # "start" or "end"
    latency_ms: float  # for "end": time from the actual last-speech sample to now


class EndpointDetector:
    def __init__(self, threshold: float, sample_rate: int, min_silence_duration_ms: int, speech_pad_ms: int):
        model = load_silero_vad()
        self._iterator = VADIterator(
            model,
            threshold=threshold,
            sampling_rate=sample_rate,
            min_silence_duration_ms=min_silence_duration_ms,
            speech_pad_ms=speech_pad_ms,
        )
        self._sample_rate = sample_rate

    def process_frame(self, frame: np.ndarray) -> VadEvent | None:
        """frame: mono float32 PCM in [-1, 1] at the configured sample rate."""
        result = self._iterator(frame, return_seconds=False)
        if result is None:
            return None
        if "end" in result:
            # current_sample is "now"; result["end"] is the sample index where speech
            # actually stopped (before the confirmation wait) - the gap between them
            # is the real endpoint decision latency.
            decision_ms = (self._iterator.current_sample - result["end"]) / self._sample_rate * 1000
            return VadEvent(kind="end", latency_ms=decision_ms)
        return VadEvent(kind="start", latency_ms=0.0)

    def reset(self) -> None:
        self._iterator.reset_states()
