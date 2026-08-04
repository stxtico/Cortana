"""Converts numbers, decimals, times, and unit abbreviations to spoken forms before
synthesis - how XTTS pronounces raw digits ("1.2mm", "2:15") is a big part of what
reads as robotic. Uses num2words (already a coqui-tts transitive dependency) for the
actual digit-to-word conversion; this module detects what needs converting (times,
decimal+unit combos, bare decimals, bare integers) and hands the number parts off to
it.

Order matters: times first (so "2:15" isn't eaten by the bare-number regexes before
it's recognized as a time), then number+unit combos, then bare decimals, then bare
integers, then acronyms last (disjoint from the digit regexes above, so order
against them doesn't matter, but keeping the pass order stable avoids re-reasoning
about it later).

Acronyms: XTTS reads an unspaced all-caps run as if it were a mispronounced regular
word, not letters - "PLA" came out as "player.a", "STL" dropped letters entirely.
Verified by transcribing (rule 6, CLAUDE.md), not just listening: spacing the
letters with periods ("P. L. A.") reliably transcribed back to the clean acronym,
while bare spaces between letters ("P L A") still garbled ("PLAware"). A small
exception list (_WORD_ACRONYMS) covers the ones actually pronounced as a single
word - confirmed "NASA" transcribes correctly read as a word. PETG looked like it
might belong on that list too but tested badly wrong both alone ("Try PETG
instead." -> "Try PG instead.") and next to PLA in one sentence (-> "Pele prints
easier than peachy.") - it's letter-spelled like everything else not on the list,
not an exception.
"""

import re

from num2words import num2words

_UNIT_WORDS = {
    "mm": "millimeter", "cm": "centimeter", "km": "kilometer", "m": "meter",
    "in": "inch", "ft": "foot", "kg": "kilogram", "g": "gram", "lb": "pound", "lbs": "pound",
}

_TIME_RE = re.compile(r'\b([01]?\d|2[0-3]):([0-5]\d)(\s*([ap])\.?m\.?)?\b', re.IGNORECASE)
_UNIT_RE = re.compile(
    r'\b(\d+(?:\.\d+)?)\s*(' + '|'.join(sorted(_UNIT_WORDS, key=len, reverse=True)) + r')\b',
    re.IGNORECASE,
)
_DECIMAL_RE = re.compile(r'\b\d+\.\d+\b')
# Guarded against matching a digit run that's actually part of a decimal - without
# this, running _INT_RE on text where a decimal survived (out-of-order use, or any
# future pipeline change) substitutes the whole and fractional parts independently
# and strands the "." - "1.2" -> "one.2", not caught by _DECIMAL_RE first. The
# correctly-ordered normalize() pipeline never hits this today (decimals/units run
# before integers, so no raw decimals remain by the time this runs) - this guard is
# for robustness if that order ever changes, not a fix for a live bug in normalize().
_INT_RE = re.compile(r'(?<!\d\.)\b\d+\b(?!\.\d)')

# All-caps 2-5 letter tokens are assumed to be initialisms (spelled letter by
# letter) unless listed here as ones actually pronounced as a single word - see
# module docstring for how this list was validated (transcribed, not just
# listened to). Deliberately short and specific rather than a dictionary lookup -
# add to it only after testing a specific acronym the same way, not by guessing.
_WORD_ACRONYMS = {"NASA", "OK"}
# Doesn't match a caps run immediately followed by a lowercase letter (a plural
# like "PLAs" or "STLs") - \b requires a transition, and "A"->"s" isn't one. Known
# gap, not handled: rare enough in practice that it wasn't worth the extra
# complexity here.
_ACRONYM_RE = re.compile(r'\b[A-Z]{2,5}\b')


def _spell_number(text: str) -> str:
    if '.' in text:
        whole, frac = text.split('.', 1)
        whole_words = num2words(int(whole)) if whole else "zero"
        frac_words = " ".join(num2words(int(d)) for d in frac)
        return f"{whole_words} point {frac_words}"
    return num2words(int(text))


def _spell_time(match: re.Match) -> str:
    hour, minute, ampm = match.group(1), match.group(2), match.group(4)
    hour_word = num2words(int(hour))
    if minute == "00":
        spoken = f"{hour_word} o'clock"
    elif minute.startswith("0"):
        spoken = f"{hour_word} oh {num2words(int(minute))}"
    else:
        spoken = f"{hour_word} {num2words(int(minute))}"
    if ampm:
        spoken += f" {ampm.lower()} m"
    return spoken


def _spell_unit(match: re.Match) -> str:
    number_words = _spell_number(match.group(1))
    unit = _UNIT_WORDS[match.group(2).lower()]
    plural = "" if number_words in ("one", "zero point one") else "s"
    return f"{number_words} {unit}{plural}"


def _spell_acronym(match: re.Match) -> str:
    word = match.group(0)
    if word in _WORD_ACRONYMS:
        return word
    spelled = ". ".join(word)
    # A trailing period after the last letter is what makes it transcribe clean
    # (bare "P L A" with no periods at all still garbled - see module docstring),
    # but if the acronym is already followed by punctuation ("PLA." at a sentence
    # end, "PLA," mid-list) adding one anyway doubles up ("P. L. A.."). Peek at
    # the source text via match.string/match.end() - re.sub's replacement
    # function only gets the Match, not surrounding context, otherwise.
    next_char = match.string[match.end():match.end() + 1]
    if next_char in ".!?,;:":
        return spelled
    return spelled + "."


def normalize(text: str) -> str:
    text = _TIME_RE.sub(_spell_time, text)
    text = _UNIT_RE.sub(_spell_unit, text)
    text = _DECIMAL_RE.sub(lambda m: _spell_number(m.group(0)), text)
    text = _INT_RE.sub(lambda m: num2words(int(m.group(0))), text)
    text = _ACRONYM_RE.sub(_spell_acronym, text)
    return text
