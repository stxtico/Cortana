"""Converts numbers, decimals, times, and unit abbreviations to spoken forms before
synthesis - how XTTS pronounces raw digits ("1.2mm", "2:15") is a big part of what
reads as robotic. Uses num2words (already a coqui-tts transitive dependency) for the
actual digit-to-word conversion; this module detects what needs converting (times,
decimal+unit combos, bare decimals, bare integers) and hands the number parts off to
it.

Order matters: times first (so "2:15" isn't eaten by the bare-number regexes before
it's recognized as a time), then number+unit combos, then bare decimals, then bare
integers last (a bare-integer regex would match the digits inside an untouched
decimal if it ran first).
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


def normalize(text: str) -> str:
    text = _TIME_RE.sub(_spell_time, text)
    text = _UNIT_RE.sub(_spell_unit, text)
    text = _DECIMAL_RE.sub(lambda m: _spell_number(m.group(0)), text)
    text = _INT_RE.sub(lambda m: num2words(int(m.group(0))), text)
    return text
