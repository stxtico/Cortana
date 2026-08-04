"""Dependency-free per-frame F0 (pitch) tracking via normalized autocorrelation.
librosa/numba can't currently resolve against this project's numpy pin - numba caps
numpy<2.5, this project needs numpy<2.5 for coqui-tts too as it turns out, but at the
time this was written the constraint was real and unresolved (see pyproject.toml).
Not YIN-grade precision - good enough as a relative signal (variance, trend), not for
exact absolute pitch.
"""

import numpy as np


def track_pitch(
    audio: np.ndarray, sample_rate: int, frame_ms: float = 40, hop_ms: float = 20,
    fmin: float = 60.0, fmax: float = 400.0, voicing_threshold: float = 0.3,
) -> list[tuple[float, float]]:
    """Returns [(time_s, f0_hz), ...] for voiced frames only - unvoiced/near-silent
    frames are skipped, not returned as zero."""
    frame_n = int(sample_rate * frame_ms / 1000)
    hop_n = int(sample_rate * hop_ms / 1000)
    min_lag = int(sample_rate / fmax)
    max_lag = int(sample_rate / fmin)
    results = []
    for i in range(0, len(audio) - frame_n + 1, hop_n):
        frame = audio[i:i + frame_n].astype(np.float64)
        frame = frame - frame.mean()
        if np.max(np.abs(frame)) < 1e-4:
            continue
        n = 1
        while n < 2 * frame_n:
            n *= 2
        spectrum = np.fft.rfft(frame, n=n)
        autocorr = np.fft.irfft(spectrum * np.conj(spectrum))[:frame_n]
        if autocorr[0] <= 0 or max_lag >= len(autocorr):
            continue
        autocorr = autocorr / autocorr[0]
        search = autocorr[min_lag:max_lag + 1]
        if len(search) == 0:
            continue
        peak_idx = int(np.argmax(search))
        if search[peak_idx] < voicing_threshold:
            continue
        lag = min_lag + peak_idx
        if lag > 0:
            results.append((i / sample_rate, sample_rate / lag))
    return results


def pitch_variance_hz(audio: np.ndarray, sample_rate: int, **kwargs) -> float | None:
    """Std of F0 across voiced frames. None if too few voiced frames to say anything."""
    f0s = [f0 for _, f0 in track_pitch(audio, sample_rate, **kwargs)]
    return float(np.std(f0s)) if len(f0s) >= 5 else None
