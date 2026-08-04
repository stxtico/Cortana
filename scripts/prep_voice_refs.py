"""Prepares XTTS voice-reference candidates from source dialogue audio (PROMPTS.md A3
step 2). For each file under [voice.prep_refs].source_dir: extract audio via ffmpeg,
segment with silero-vad's offline batch segmenter (get_speech_timestamps - not
services/ears/vad.py's EndpointDetector, which is built for streaming, not a whole
file), drop segments outside [min_duration_s, max_duration_s], score the rest for
cleanliness (RMS level, noise floor from leading/trailing silence, spectral flatness),
transcribe with faster-whisper to confirm clean single-speaker speech, and reject on
threshold. Those filters always gate which segments are even candidates.

Two ranking modes on top of that pool, chosen with --rank-by:
  snr  (default) - rank survivors by SNR, export top_n to export_dir/. Acoustic
        quality only; says nothing about delivery.
  calm - rank survivors by a combined calmness score (low RMS variance, moderate
        absolute level, low pitch variance, slow speech rate - see
        _rms_variance_db/_pitch_variance_hz/_speech_rate_cps), export calm_top_n to
        export_dir/calm/ so an existing snr-mode export isn't touched. Also flags
        likely comms/radio-filtered segments in the manifest (band-limited energy -
        XTTS clones that EQ permanently, so they're bad references even when calm).

Neither mode picks a final reference - the metrics are good at rejecting bad clips
(noise, silence, garbled speech, shouting) and bad at predicting which good clip
clones best, so that call is made by ear, not by score.

Separately: every VAD segment (any duration, not just voice-ref candidates) whose
transcript contains the wake phrase gets exported as a hard negative for the wake
model, alongside services/ears/hard_negatives/ - real dialogue saying the assistant's
name is exactly the kind of hard negative wake_calibration.py can only find live. This
only runs in --rank-by snr (the default, first) pass - it's a full-corpus sweep
independent of ranking mode, so a calm re-rank doesn't re-append duplicate entries.
"""

import argparse
import json
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
from scipy.io import wavfile
from silero_vad import get_speech_timestamps, load_silero_vad

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.ears.pitch import pitch_variance_hz
from services.ears.stt import Transcriber

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"
SOURCE_EXTENSIONS = {".mp4", ".mp3", ".wav", ".m4a", ".flac", ".mkv", ".webm", ".mov"}

HARD_NEG_DIR = ROOT / "services" / "ears" / "hard_negatives"
HARD_NEG_MANIFEST = HARD_NEG_DIR / "manifest_voice_refs.jsonl"


@dataclass
class Candidate:
    source_file: str
    start_s: float
    end_s: float
    duration_s: float
    rms_db: float
    noise_floor_db: float
    snr_db: float
    spectral_flatness: float
    avg_logprob: float
    no_speech_prob: float
    transcript: str
    # --rank-by calm only; None in --rank-by snr (default).
    rms_variance_db: float | None = None
    pitch_variance_hz: float | None = None
    speech_rate_cps: float | None = None
    calmness_score: float | None = None
    in_band_energy_frac: float | None = None
    radio_filtered: bool | None = None


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config["audio"], config["voice"]["prep_refs"]


def _slug(text: str, max_len: int = 40) -> str:
    keep = "".join(c if c.isalnum() else "_" for c in text)
    return keep.strip("_")[:max_len] or "clip"


def _extract_audio(path: Path, sample_rate: int) -> np.ndarray:
    """Mono float32 PCM at sample_rate, decoded via ffmpeg (handles mp4/mkv/etc, not
    just formats scipy/soundfile can read directly)."""
    cmd = [
        "ffmpeg", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "wav", "-loglevel", "error", "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=True)
    from io import BytesIO
    sr, audio = wavfile.read(BytesIO(proc.stdout))
    assert sr == sample_rate
    return audio.astype(np.float32) / 32768.0


_INT16_QUANTIZATION_FLOOR = 1.0 / 32768  # ~-90.3dB - source audio's real noise floor from
# 16-bit quantization. Using a tinier eps (e.g. 1e-10) lets hard digital silence between
# game-audio lines (exact zero samples, no natural room tone) register as ~-200dB, which
# blows SNR up past 150dB and makes the ranking meaningless - silence isn't "clean," it's
# just absent.


def _rms_db(audio: np.ndarray, eps: float = _INT16_QUANTIZATION_FLOOR) -> float:
    rms = np.sqrt(np.mean(audio.astype(np.float64) ** 2))
    return 20 * np.log10(rms + eps)


def _noise_floor_db(audio: np.ndarray, sample_rate: int, start_sample: int, end_sample: int,
                     context_ms: int = 200) -> float:
    """RMS of whatever leading/trailing context is available just outside the segment
    - the closest thing to a noise-floor sample without a separate silence pass."""
    context_n = int(sample_rate * context_ms / 1000)
    before = audio[max(0, start_sample - context_n):start_sample]
    after = audio[end_sample:min(len(audio), end_sample + context_n)]
    context = np.concatenate([c for c in (before, after) if len(c) > 0])
    if len(context) == 0:
        return _rms_db(audio)  # no context available (segment touches file edge) - fall back
    return _rms_db(context)


def _spectral_flatness(audio: np.ndarray, frame_size: int = 2048, hop: int = 1024, eps: float = 1e-10) -> float:
    """Mean over frames of geometric-mean(power) / arithmetic-mean(power). Near 0 =
    tonal/voiced (formant peaks), near 1 = flat/noise-like."""
    if len(audio) < frame_size:
        frame_size = len(audio)
        hop = frame_size
    if frame_size == 0:
        return 1.0
    flatness_values = []
    for i in range(0, len(audio) - frame_size + 1, hop):
        frame = audio[i:i + frame_size]
        power = np.abs(np.fft.rfft(frame * np.hanning(len(frame)))) ** 2 + eps
        geo_mean = np.exp(np.mean(np.log(power)))
        arith_mean = np.mean(power)
        flatness_values.append(geo_mean / arith_mean)
    return float(np.mean(flatness_values)) if flatness_values else 1.0


def _rms_variance_db(audio: np.ndarray, sample_rate: int, frame_ms: float = 50, hop_ms: float = 25) -> float:
    """Std of per-frame RMS (dB) across the segment. Shouting/urgency spikes loudness;
    even, calm delivery stays flat."""
    frame_n = max(1, int(sample_rate * frame_ms / 1000))
    hop_n = max(1, int(sample_rate * hop_ms / 1000))
    frame_dbs = [_rms_db(audio[i:i + frame_n]) for i in range(0, len(audio) - frame_n + 1, hop_n)]
    return float(np.std(frame_dbs)) if len(frame_dbs) >= 2 else 0.0


def _speech_rate_cps(transcript_text: str, duration_s: float) -> float:
    return len(transcript_text.strip()) / duration_s if duration_s > 0 else 0.0


def _band_energy_fraction(audio: np.ndarray, sample_rate: int, low_hz: float, high_hz: float,
                           frame_size: int = 2048, hop: int = 1024) -> float:
    """Mean fraction of spectral power inside [low_hz, high_hz] across frames. Near 1.0
    means almost all energy is band-limited - the comms/radio-filter signature. Real
    full-bandwidth speech has measurable energy below 300Hz (F0 fundamental for most
    voices sits there) so this stays well under 1.0 for an unfiltered recording."""
    if len(audio) < frame_size:
        frame_size = len(audio)
        hop = frame_size
    if frame_size == 0:
        return 0.0
    freqs = np.fft.rfftfreq(frame_size, d=1 / sample_rate)
    band_mask = (freqs >= low_hz) & (freqs <= high_hz)
    fractions = []
    for i in range(0, len(audio) - frame_size + 1, hop):
        frame = audio[i:i + frame_size]
        power = np.abs(np.fft.rfft(frame * np.hanning(len(frame)))) ** 2
        total = power.sum()
        if total > 0:
            fractions.append(power[band_mask].sum() / total)
    return float(np.mean(fractions)) if fractions else 0.0


def _to_int16(audio: np.ndarray) -> np.ndarray:
    return np.clip(audio * 32768.0, -32768, 32767).astype(np.int16)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rank-by", choices=["snr", "calm"], default="snr",
                         help="snr (default): acoustic quality only, exports to export_dir/. "
                              "calm: calm/level delivery, exports to export_dir/calm/ without "
                              "touching an existing snr-mode export.")
    parser.add_argument("--top-n", type=int, default=None, help="override config top_n/calm_top_n")
    args = parser.parse_args()
    rank_by = args.rank_by

    audio_cfg, prep_cfg = _load_config()
    analysis_sr = audio_cfg["sample_rate"]
    vad_cfg = audio_cfg["vad"]
    wake_phrase = audio_cfg["wake"]["verify_phrase"].lower()

    source_dir = ROOT / prep_cfg["source_dir"]
    export_dir = ROOT / prep_cfg["export_dir"] if rank_by == "snr" else ROOT / prep_cfg["export_dir"] / "calm"
    export_sr = prep_cfg["export_sample_rate"]
    min_duration_s = prep_cfg["min_duration_s"]
    max_duration_s = prep_cfg["max_duration_s"]
    top_n = args.top_n if args.top_n is not None else (
        prep_cfg["top_n"] if rank_by == "snr" else prep_cfg["calm_top_n"]
    )
    min_snr_db = prep_cfg["min_snr_db"]
    max_spectral_flatness = prep_cfg["max_spectral_flatness"]
    min_avg_logprob = prep_cfg["min_avg_logprob"]
    max_no_speech_prob = prep_cfg["max_no_speech_prob"]
    radio_low_hz = prep_cfg["radio_band_low_hz"]
    radio_high_hz = prep_cfg["radio_band_high_hz"]
    radio_threshold = prep_cfg["radio_in_band_threshold"]

    source_files = sorted(p for p in source_dir.iterdir() if p.suffix.lower() in SOURCE_EXTENSIONS)
    if not source_files:
        raise SystemExit(f"No source audio/video found in {source_dir} (looked for {SOURCE_EXTENSIONS})")
    print(f"Found {len(source_files)} source file(s) in {source_dir}: {[p.name for p in source_files]}")

    vad_model = load_silero_vad()
    stt = Transcriber(
        model_name=audio_cfg["stt"]["model"], device=audio_cfg["stt"]["device"],
        compute_type=audio_cfg["stt"]["compute_type"], language=audio_cfg["stt"]["language"],
    )

    all_candidates: list[tuple[Candidate, np.ndarray]] = []  # paired with export-quality audio slice
    hard_negatives_found = 0

    for source_path in source_files:
        print(f"\n--- {source_path.name} ---")
        print("extracting audio...")
        analysis_audio = _extract_audio(source_path, analysis_sr)
        export_audio = _extract_audio(source_path, export_sr)

        timestamps = get_speech_timestamps(
            torch.from_numpy(analysis_audio), vad_model,
            threshold=vad_cfg["threshold"], sampling_rate=analysis_sr,
            min_silence_duration_ms=vad_cfg["min_silence_duration_ms"],
            speech_pad_ms=vad_cfg["speech_pad_ms"],
        )
        print(f"{len(timestamps)} raw speech segments found")

        for i, ts in enumerate(timestamps):
            start_sample, end_sample = ts["start"], ts["end"]
            start_s, end_s = start_sample / analysis_sr, end_sample / analysis_sr
            duration_s = end_s - start_s

            segment_audio = analysis_audio[start_sample:end_sample]
            transcript = stt.transcribe(segment_audio)

            if rank_by == "snr" and wake_phrase in transcript.text.lower():
                hard_negatives_found += 1
                HARD_NEG_DIR.mkdir(parents=True, exist_ok=True)
                fname = f"voiceref_{source_path.stem}_{i:04d}_{_slug(transcript.text)}.wav"
                wavfile.write(HARD_NEG_DIR / fname, analysis_sr, _to_int16(segment_audio))
                with HARD_NEG_MANIFEST.open("a") as f:
                    f.write(json.dumps({
                        "file": fname, "source_file": source_path.name, "start_s": round(start_s, 2),
                        "end_s": round(end_s, 2), "transcript": transcript.text,
                        "saved_at": datetime.now(timezone.utc).isoformat(),
                    }) + "\n")

            if not (min_duration_s <= duration_s <= max_duration_s):
                continue

            rms_db = _rms_db(segment_audio)
            noise_floor_db = _noise_floor_db(analysis_audio, analysis_sr, start_sample, end_sample)
            snr_db = rms_db - noise_floor_db
            flatness = _spectral_flatness(segment_audio)

            passed = (
                snr_db >= min_snr_db
                and flatness <= max_spectral_flatness
                and transcript.avg_logprob >= min_avg_logprob
                and transcript.no_speech_prob <= max_no_speech_prob
            )
            print(f"  [{i:04d}] {duration_s:5.1f}s snr={snr_db:5.1f}dB flat={flatness:.3f} "
                  f"logprob={transcript.avg_logprob:+.2f} no_speech={transcript.no_speech_prob:.2f} "
                  f"{'PASS' if passed else 'reject'} '{transcript.text[:60]}'")
            if not passed:
                continue

            candidate = Candidate(
                source_file=source_path.name, start_s=round(start_s, 3), end_s=round(end_s, 3),
                duration_s=round(duration_s, 3), rms_db=round(rms_db, 2),
                noise_floor_db=round(noise_floor_db, 2), snr_db=round(snr_db, 2),
                spectral_flatness=round(flatness, 4), avg_logprob=round(transcript.avg_logprob, 3),
                no_speech_prob=round(transcript.no_speech_prob, 3), transcript=transcript.text,
            )

            if rank_by == "calm":
                candidate.rms_variance_db = round(_rms_variance_db(segment_audio, analysis_sr), 3)
                pitch_var = pitch_variance_hz(segment_audio, analysis_sr)
                candidate.pitch_variance_hz = round(pitch_var, 2) if pitch_var is not None else None
                candidate.speech_rate_cps = round(_speech_rate_cps(transcript.text, duration_s), 2)
                in_band = _band_energy_fraction(segment_audio, analysis_sr, radio_low_hz, radio_high_hz)
                candidate.in_band_energy_frac = round(in_band, 4)
                candidate.radio_filtered = in_band >= radio_threshold

            export_start = round(start_s * export_sr)
            export_end = round(end_s * export_sr)
            all_candidates.append((candidate, export_audio[export_start:export_end]))

    if rank_by == "calm" and all_candidates:
        rms_var_arr = np.array([c.rms_variance_db for c, _ in all_candidates])
        rms_db_arr = np.array([c.rms_db for c, _ in all_candidates])
        median_rms_db = float(np.median(rms_db_arr))
        moderate_arr = -np.abs(rms_db_arr - median_rms_db)  # already "higher=calmer" oriented

        pitch_raw = [c.pitch_variance_hz for c, _ in all_candidates]
        known = [p for p in pitch_raw if p is not None]
        fallback = float(np.mean(known)) if known else 0.0
        pitch_arr = np.array([p if p is not None else fallback for p in pitch_raw])  # missing -> neutral z=0

        rate_arr = np.array([c.speech_rate_cps for c, _ in all_candidates])

        def _z(arr: np.ndarray) -> np.ndarray:
            std = arr.std()
            return (arr - arr.mean()) / std if std > 1e-9 else np.zeros_like(arr)

        # rms_variance, pitch_variance, speech_rate: lower = calmer, so negate their z-score.
        # moderate_arr is already oriented higher=calmer, no negation.
        calmness = (-_z(rms_var_arr) + _z(moderate_arr) - _z(pitch_arr) - _z(rate_arr)) / 4.0
        for (candidate, _), score in zip(all_candidates, calmness):
            candidate.calmness_score = round(float(score), 4)

        all_candidates.sort(key=lambda pair: pair[0].calmness_score, reverse=True)
        radio_flagged = sum(1 for c, _ in all_candidates if c.radio_filtered)
    else:
        all_candidates.sort(key=lambda pair: pair[0].snr_db, reverse=True)
        radio_flagged = 0

    kept = all_candidates[:top_n]

    if len(all_candidates) < top_n:
        print(f"\nOnly {len(all_candidates)} candidate(s) passed cleanliness filters - "
              f"fewer than the requested top_n={top_n}. Exporting all of them.")

    export_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = export_dir / "manifest.jsonl"
    with manifest_path.open("w") as f:
        for rank, (candidate, export_slice) in enumerate(kept, start=1):
            fname = f"voice_ref_{rank:02d}.wav"
            wavfile.write(export_dir / fname, export_sr, _to_int16(export_slice))
            f.write(json.dumps({"rank": rank, "file": fname, **asdict(candidate)}) + "\n")

    print(f"\nExported {len(kept)} candidate(s) to {export_dir} ({manifest_path.name} has full metrics).")
    if rank_by == "snr":
        print(f"Flagged {hard_negatives_found} wake-phrase hard negative(s) to {HARD_NEG_DIR} "
              f"({HARD_NEG_MANIFEST.name}).")
    else:
        print(f"{radio_flagged} of {len(all_candidates)} passing candidate(s) flagged as likely "
              f"comms/radio-filtered (in_band_energy_frac >= {radio_threshold}) - check the manifest "
              f"before picking one even if it ranked well on calmness.")
    print("No reference was picked - audition 3-5 by ear before choosing (see PROMPTS.md A3 step 2).")


if __name__ == "__main__":
    main()
