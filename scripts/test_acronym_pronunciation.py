"""Tests candidate acronyms for normalize.py's word-vs-letters exception list the
same way PETG was tested: synthesize both the raw (unspelled) form and the
letter-spelled form ("P. L. A.") in a short natural sentence, transcribe both,
and check which one the transcript recovers correctly. PETG looked like it might
be a word on paper and tested badly both ways when actually spelled out; this
script exists so the same mistake isn't made by eyeballing the others.

Verdict heuristic: "word" wins if the raw form's transcript contains the intended
word/acronym read naturally (case-insensitive substring match on the acronym
letters as a contiguous run, e.g. "cad" in "the cad job"). "letters" wins if the
spelled form's transcript reconstructs the acronym (Whisper's own behavior when
it hears genuine spelled-out letters is to write the acronym back out, as seen
in the PLA/PETG testing) AND the raw form's transcript does NOT read correctly.
Ambiguous cases (neither or both read correctly) are flagged for a human call.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tomllib
import torch
import torchaudio

from services.ears.stt import Transcriber
from services.voice.xtts_engine import XTTSEngine

CANDIDATES = {
    "CAD": "The {} job finished about an hour ago.",
    "STEP": "The {} file is ready for review.",
    "LASER": "The {} is calibrated correctly.",
    "RADAR": "The {} detected the object early.",
    "SCUBA": "She went {} diving yesterday.",
    "GIF": "Save it as a {} file.",
    "JPEG": "Save it as a {} file.",
    "PNG": "Save it as a {} file.",
    "RAM": "The computer needs more {}.",
    "ROM": "The data is stored in {}.",
}


def spell(word: str) -> str:
    return ". ".join(word) + "."


def main() -> None:
    with open("config/cortana.toml", "rb") as f:
        cfg = tomllib.load(f)
    voice_cfg = cfg["voice"]["xtts"]
    stt_cfg = cfg["audio"]["stt"]

    print("Loading XTTS...")
    eng = XTTSEngine(
        references=voice_cfg["references"], default_reference=voice_cfg.get("default_reference", "calm"),
        language=voice_cfg.get("language", "en"), device=voice_cfg.get("device", "cuda"),
    )
    print("Loading transcriber...")
    tr = Transcriber(
        model_name=stt_cfg["model"], device=stt_cfg["device"],
        compute_type=stt_cfg["compute_type"], language=stt_cfg["language"],
    )

    results = {}
    for word, template in CANDIDATES.items():
        raw_text = template.format(word)
        spelled_text = template.format(spell(word))

        raw_audio = eng.synthesize(raw_text)
        raw_16k = torchaudio.functional.resample(
            torch.from_numpy(raw_audio).unsqueeze(0), eng.sample_rate, 16000
        ).squeeze(0).numpy()
        raw_transcript = tr.transcribe(raw_16k).text

        spelled_audio = eng.synthesize(spelled_text)
        spelled_16k = torchaudio.functional.resample(
            torch.from_numpy(spelled_audio).unsqueeze(0), eng.sample_rate, 16000
        ).squeeze(0).numpy()
        spelled_transcript = tr.transcribe(spelled_16k).text

        # "word" reading recovered: the acronym's letters appear as a contiguous
        # run (case-insensitive) in the raw transcript - i.e. Whisper heard
        # something that reads back as the word/acronym itself.
        word_ok = bool(re.search(re.escape(word), raw_transcript, re.IGNORECASE))
        # "letters" reading recovered: Whisper's own behavior on genuine spelled
        # speech is to write the acronym back out (established in the PLA/PETG
        # testing) - same check against the spelled-form transcript.
        letters_ok = bool(re.search(re.escape(word), spelled_transcript, re.IGNORECASE))

        if word_ok and not letters_ok:
            verdict = "WORD"
        elif letters_ok and not word_ok:
            verdict = "LETTERS"
        elif word_ok and letters_ok:
            verdict = "AMBIGUOUS (both read correctly)"
        else:
            verdict = "AMBIGUOUS (neither read correctly)"

        results[word] = {
            "raw_text": raw_text, "raw_transcript": raw_transcript,
            "spelled_text": spelled_text, "spelled_transcript": spelled_transcript,
            "verdict": verdict,
        }
        print(f"\n=== {word} ===")
        print(f"  raw:     {raw_text!r:60} -> {raw_transcript!r}")
        print(f"  spelled: {spelled_text!r:60} -> {spelled_transcript!r}")
        print(f"  VERDICT: {verdict}")

    print("\n\n=== Summary ===")
    for word, r in results.items():
        print(f"{word:8} {r['verdict']}")


if __name__ == "__main__":
    main()
