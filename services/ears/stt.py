"""faster-whisper large-v3-turbo. Transcribes whole VAD-segmented utterances, not a
live stream - whisper needs the full audio, not a token at a time.
"""

import os
import sysconfig
import time
from dataclasses import dataclass

import numpy as np
from faster_whisper import WhisperModel


def _add_cuda_dll_dirs() -> None:
    # ctranslate2 doesn't find cuBLAS/cuDNN on PATH on Windows unless the pip-installed
    # nvidia-* packages' DLL directories are added explicitly.
    if os.name != "nt":
        return
    site_packages = sysconfig.get_paths()["purelib"]
    for pkg in ("cublas", "cudnn", "cuda_nvrtc"):
        bin_dir = os.path.join(site_packages, "nvidia", pkg, "bin")
        if os.path.isdir(bin_dir):
            os.add_dll_directory(bin_dir)
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]


_add_cuda_dll_dirs()


@dataclass
class Transcript:
    text: str
    latency_ms: float


class Transcriber:
    def __init__(self, model_name: str, device: str, compute_type: str, language: str):
        self._model = WhisperModel(model_name, device=device, compute_type=compute_type)
        self._language = language

    def transcribe(self, audio: np.ndarray) -> Transcript:
        """audio: mono float32 PCM at the model's expected 16kHz sample rate."""
        start = time.perf_counter()
        segments, _ = self._model.transcribe(audio, language=self._language)
        text = "".join(segment.text for segment in segments).strip()
        latency_ms = (time.perf_counter() - start) * 1000
        return Transcript(text=text, latency_ms=latency_ms)
