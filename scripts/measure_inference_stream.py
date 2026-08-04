"""Measures Xtts.inference_stream() directly - whole-text conditioning (text is fed
as one sequence when enable_text_splitting=False, confirmed by reading the source)
plus progressive chunked audio output, which could deliver both the prosody win and
fast first audio without speak_stream()'s chunking gymnastics. Same test text/
reference as the hybrid/hybrid3 measurements for direct comparability.

This bypasses XTTSEngine.synthesize() (which only wraps the non-streaming
inference()) and calls the model directly - inference_stream() is a synchronous
generator, not something XTTSEngine currently exposes. This script only measures
raw characteristics (time to first chunk, whether generation keeps pace with
playback); wiring it into the async speak_stream() pipeline is a follow-up if the
numbers justify it.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import soundfile as sf

from services.voice.normalize import normalize
from services.voice.tts import sanitize
from services.voice.xtts_engine import XTTSEngine

ROOT = Path(__file__).resolve().parent.parent
REF_PATH = ROOT / "voice_refs" / "calm" / "voice_ref_14.wav"
OUT_DIR = ROOT / "voice_refs" / "audition" / "inference_stream_test"
SAMPLE_RATE = 24000

RESPONSE = (
    "Got it. "
    "Your meeting's at three, and the CAD job finished about an hour ago. "
    "The export looks clean, no errors in the log, and the STEP file is ready for review. "
    "I'll flag it if anything changes before then."
)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    text = normalize(sanitize(RESPONSE))
    print(f"text ({len(text)} chars): {text!r}")

    print("\nLoading XTTS v2...")
    engine = XTTSEngine()
    engine.set_reference(str(REF_PATH))

    for run in range(2):
        print(f"\n=== run {run} ===")
        chunks = []
        chunk_times = []
        t_start = time.perf_counter()
        gpt_latent = engine._gpt_cond_latent
        speaker_emb = engine._speaker_embedding

        stream = engine._model.inference_stream(
            text, engine._language, gpt_latent, speaker_emb,
        )
        first_chunk_time = None
        for wav_chunk in stream:
            now = time.perf_counter()
            if first_chunk_time is None:
                first_chunk_time = now
                print(f"  time to first chunk: {(now - t_start) * 1000:.1f}ms")
            arr = wav_chunk.detach().cpu().numpy().astype(np.float32)
            chunk_dur_s = len(arr) / SAMPLE_RATE
            chunk_times.append((now - t_start, chunk_dur_s))
            chunks.append(arr)

        total_wall_s = time.perf_counter() - t_start
        full_audio = np.concatenate(chunks)
        total_audio_s = len(full_audio) / SAMPLE_RATE
        print(f"  {len(chunks)} chunks, total wall time={total_wall_s:.2f}s, total audio={total_audio_s:.2f}s")
        print(f"  generation-to-playback ratio: {total_wall_s / total_audio_s:.2f}x "
              f"({'faster than real-time - can stay ahead of playback' if total_wall_s < total_audio_s else 'SLOWER than real-time - will fall behind playback'})")

        print("  per-chunk arrival times (wall_s) vs cumulative audio duration so far:")
        cumulative_audio = 0.0
        for i, (arrival_s, dur_s) in enumerate(chunk_times):
            cumulative_audio += dur_s
            behind = arrival_s - cumulative_audio
            flag = "  <-- generation is BEHIND where playback would be" if behind > 0 else ""
            print(f"    chunk {i:2d} arrived at {arrival_s:6.3f}s, +{dur_s:.3f}s audio, "
                  f"cumulative_audio={cumulative_audio:.3f}s, arrival-vs-audio={behind:+.3f}s{flag}")

        out_path = OUT_DIR / f"run{run}.wav"
        sf.write(out_path, full_audio, SAMPLE_RATE)
        print(f"  saved: {out_path.name}")

    engine.close()
    print(f"\nDone. Files in {OUT_DIR}")


if __name__ == "__main__":
    main()
