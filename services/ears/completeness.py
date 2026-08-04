"""Does a just-ended utterance look like a finished thought or an abandoned one?

Originally scoped as a call to [models].fast, but VRAM on the 3080 Ti is already over
capacity with just gemma4:12b + Whisper + XTTS resident (CLAUDE.md has the measured
numbers - a second model reliably causes eviction/reload, and the base stack alone
already fails intermittently). So this is a non-LLM heuristic instead: cheap, no GPU
call, no additional VRAM.

Internal signal only. The caller (services/ears/backchannel.py) decides what to do
with it - never surface the prediction as spoken output, and never assume it knows
what the user was going to say next.

Two signals on by default, combined by simple OR - each is designed to be a strong,
mostly unambiguous cue on its own, not something that needs corroboration:
- dangling_word: the transcript's last word grammatically expects a continuation
  (conjunction, preposition, article, dangling auxiliary verb - "so I was...", "the
  meeting's at...").
- filler_ending: trails off on a hesitation filler ("um", "uh", "like").

A third signal, prosody (F0 trend over the last tail_s of speech - falling reads as
statement-completion, flat/rising as a continuation or trailing question), exists in
code but is OFF by default (use_prosody=False). Validated against real captured
hesitation audio (logs/vad_pause_test/), not synthetic TTS output - a hand-rolled
autocorrelation pitch tracker (services/ears/pitch.py; librosa/numba can't resolve
against this project's numpy pin) called 14 of 17 real segments "flat_or_rising",
including clean, grammatically-complete sentences like "And I remember I was doing
the calisthenics." and "went back home." - it was overriding correct text verdicts
with noise more often than it added signal. Text-only signals classified that same
set correctly except for a couple of genuine hard cases (a bare "doing" with no
following dangling word, and "in a specific order" needing more context than any
local signal can supply). Left in and configurable in case a better pitch tracker
becomes available later, but don't turn it on without re-validating the same way.
"""

from dataclasses import dataclass, field

import numpy as np

from services.ears.pitch import track_pitch

_DANGLING_WORDS = frozenset({
    # conjunctions/connectors - grammatically expect a continuation
    "and", "but", "or", "so", "because", "although", "though", "since", "while",
    "if", "when", "unless", "whereas", "nor", "yet", "than", "as", "then",
    # prepositions
    "in", "on", "at", "with", "to", "for", "of", "from", "about", "into", "onto",
    "over", "under", "before", "after", "between", "through", "during", "without",
    "within", "against", "among", "along", "across", "behind", "beside", "beyond",
    "despite", "except", "near", "toward", "towards", "upon", "via",
    # articles/determiners - expect a noun to follow
    "a", "an", "the", "my", "his", "her", "their", "its", "our", "your", "this",
    "that", "these", "those", "some", "any", "each", "every", "no",
    # auxiliary/copula verbs left dangling - expect a verb phrase to follow
    "is", "are", "was", "were", "am", "be", "been", "being", "has", "have", "had",
    "will", "would", "shall", "should", "can", "could", "may", "might", "must",
    "do", "does", "did", "not",
})

_FILLER_WORDS = frozenset({"um", "uh", "umm", "uhh", "hmm", "er", "ah", "like", "well"})


@dataclass
class CompletenessResult:
    complete: bool
    signals: dict = field(default_factory=dict)  # for logging - which signal(s) fired


def _last_word(text: str) -> str:
    stripped = text.strip().rstrip(".,!?;:—-")
    words = stripped.split()
    return words[-1].lower() if words else ""


def _trim_trailing_silence(audio: np.ndarray, sample_rate: int, frame_ms: float = 20,
                            rms_floor_ratio: float = 0.1) -> np.ndarray:
    """The audio a VAD endpoint hands over includes min_silence_duration_ms (600ms by
    default) of confirmed trailing silence by construction - without trimming it, a
    'last tail_s' window would grab mostly that silence instead of the actual
    sentence-final pitch contour."""
    frame_n = max(1, int(sample_rate * frame_ms / 1000))
    if len(audio) < frame_n:
        return audio
    frame_rms = [
        np.sqrt(np.mean(audio[i:i + frame_n].astype(np.float64) ** 2))
        for i in range(0, len(audio) - frame_n + 1, frame_n)
    ]
    peak_rms = max(frame_rms) if frame_rms else 0.0
    if peak_rms <= 0:
        return audio
    floor = peak_rms * rms_floor_ratio
    last_voiced_end = 0
    for i, rms in enumerate(frame_rms):
        if rms >= floor:
            last_voiced_end = (i + 1) * frame_n
    return audio[:last_voiced_end] if last_voiced_end > 0 else audio


def _pitch_trend(audio: np.ndarray, sample_rate: int, tail_s: float, fall_threshold: float,
                  min_voiced_frames: int) -> str | None:
    """'falling', 'flat_or_rising', or None (inconclusive - too little voiced audio
    in the tail). fall_threshold is a fractional-decline-per-second rate (e.g. 0.3 =
    F0 must be dropping at least 30%/second, relative to its own median, to count as
    falling) - a rate rather than a fixed Hz threshold so it isn't voice-register-
    dependent."""
    trimmed = _trim_trailing_silence(audio, sample_rate)
    tail_start = max(0, len(trimmed) - int(tail_s * sample_rate))
    frames = track_pitch(trimmed[tail_start:], sample_rate)
    if len(frames) < min_voiced_frames:
        return None

    ts = np.array([t for t, _ in frames])
    f0s = np.array([f0 for _, f0 in frames])

    # Reject likely octave errors - autocorrelation's most common failure mode. A
    # single speaker's F0 within one short utterance tail shouldn't swing more than
    # roughly an octave; anything that does is almost always a halved/doubled
    # estimate, not genuine pitch movement.
    median_f0 = np.median(f0s)
    keep = (f0s > median_f0 * 0.67) & (f0s < median_f0 * 1.5)
    if keep.sum() < min_voiced_frames:
        return None
    ts, f0s = ts[keep], f0s[keep]

    slope = np.polyfit(ts, f0s, 1)[0]  # Hz/s
    relative_slope = slope / np.median(f0s)  # fractional change per second
    return "falling" if relative_slope < -fall_threshold else "flat_or_rising"


def check_completeness(
    text: str, audio: np.ndarray, sample_rate: int, use_prosody: bool = False,
    tail_s: float = 0.6, pitch_fall_threshold: float = 0.3, min_voiced_frames: int = 4,
) -> CompletenessResult:
    """audio: the full utterance's mono float32 PCM at sample_rate - only the tail
    is used for the prosody signal, but the whole thing is accepted so the caller
    doesn't have to slice it. use_prosody defaults off - see module docstring."""
    word = _last_word(text)
    dangling = word in _DANGLING_WORDS
    filler = word in _FILLER_WORDS
    trend = _pitch_trend(audio, sample_rate, tail_s, pitch_fall_threshold, min_voiced_frames) if use_prosody else None
    prosody_incomplete = trend == "flat_or_rising"

    complete = not (dangling or filler or prosody_incomplete)
    return CompletenessResult(complete=complete, signals={
        "last_word": word, "dangling_word": dangling, "filler_ending": filler,
        "pitch_trend": trend,
    })
