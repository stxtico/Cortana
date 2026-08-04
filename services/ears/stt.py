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
    # ctranslate2 doesn't find cuBLAS on PATH on Windows unless the pip-installed
    # nvidia-cublas-cu12 package's DLL directory is added explicitly. Deliberately
    # NOT doing the same for cudnn: adding the standalone nvidia-cudnn-cu12 package's
    # directory alongside torch's own bundled cuDNN (torch/lib/*cudnn*) causes Windows
    # to mix DLLs from both installations - torch's bundle is missing
    # cudnn_ext64_9.dll and cudnn_engines_tensor_ir64_9.dll, so anything that needs
    # those falls through to the standalone package's copies, which don't match the
    # core DLLs already loaded from torch's bundle. Confirmed by reproduction: XTTS's
    # conditioning-latent step (a torchaudio resample using cuDNN's graph API) failed
    # with CUDNN_STATUS_SUBLIBRARY_VERSION_MISMATCH the instant this module was even
    # imported in the same process - not a load, not a call, just the import, and
    # independent of free VRAM (reproduced with 10GB+ free). Every real call site in
    # this project already has torch loaded in-process by the time Whisper needs
    # cuDNN (services/ears/vad.py's silero-vad pulls it in, as does XTTS directly), so
    # ctranslate2 finding cuDNN via torch's own registered DLL directory instead of a
    # second copy is sufficient - verified with a real transcribe() call, not just a
    # successful model load.
    if os.name != "nt":
        return
    site_packages = sysconfig.get_paths()["purelib"]
    for pkg in ("cublas", "cuda_nvrtc"):
        bin_dir = os.path.join(site_packages, "nvidia", pkg, "bin")
        if os.path.isdir(bin_dir):
            os.add_dll_directory(bin_dir)
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]


_add_cuda_dll_dirs()


@dataclass
class Transcript:
    text: str
    latency_ms: float
    avg_logprob: float = 0.0     # faster-whisper per-segment confidence, averaged; ~0 = confident, more negative = less
    no_speech_prob: float = 0.0  # averaged; near 1 = likely not speech at all


class Transcriber:
    def __init__(self, model_name: str, device: str, compute_type: str, language: str):
        self._model = WhisperModel(model_name, device=device, compute_type=compute_type)
        self._language = language

    def transcribe(self, audio: np.ndarray) -> Transcript:
        """audio: mono float32 PCM at the model's expected 16kHz sample rate."""
        start = time.perf_counter()
        segments = list(self._model.transcribe(audio, language=self._language)[0])
        text = "".join(segment.text for segment in segments).strip()
        latency_ms = (time.perf_counter() - start) * 1000
        if segments:
            avg_logprob = sum(s.avg_logprob for s in segments) / len(segments)
            no_speech_prob = sum(s.no_speech_prob for s in segments) / len(segments)
        else:
            avg_logprob, no_speech_prob = 0.0, 1.0
        return Transcript(text=text, latency_ms=latency_ms, avg_logprob=avg_logprob, no_speech_prob=no_speech_prob)
