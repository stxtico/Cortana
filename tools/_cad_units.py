"""Unit parsing for tools/cad.py (PROMPTS.md A14 - "unit checking, flag any
bare number and any mixed-system arithmetic"). Every dimension the model
passes to cad_generate must be a string with an explicit unit
("40mm", "0.25in") - never a bare number ("40"). Converting everything to
mm here, in Python, before a single number reaches the LLM-generated
CadQuery code, prevents mixed-system arithmetic structurally (the model
never sees a raw inch value to accidentally combine with a raw mm value) -
a stronger guarantee than trying to detect the bug after the fact in
generated code, which would mean pattern-matching unitless floats and
guessing which convention each one was meant in.
"""

import re

UNIT_TO_MM = {
    "mm": 1.0,
    "millimeter": 1.0,
    "millimeters": 1.0,
    "cm": 10.0,
    "centimeter": 10.0,
    "centimeters": 10.0,
    "m": 1000.0,
    "meter": 1000.0,
    "meters": 1000.0,
    "in": 25.4,
    "inch": 25.4,
    "inches": 25.4,
    '"': 25.4,
    "ft": 304.8,
    "foot": 304.8,
    "feet": 304.8,
    "'": 304.8,
}

# Longest unit tokens first so "inches" matches before "in" would short-circuit it.
_UNIT_PATTERN = "|".join(sorted((re.escape(u) for u in UNIT_TO_MM), key=len, reverse=True))
_DIMENSION_RE = re.compile(rf"^\s*(-?\d+(?:\.\d+)?)\s*({_UNIT_PATTERN})\s*$", re.IGNORECASE)


def parse_dimension_mm(value: str) -> float:
    """Parses one dimension string ("40mm", "0.25 in") to a float in mm.
    Raises ValueError with a message naming exactly what's wrong - either
    "no unit" (a bare number) or "unrecognized unit" - so the caller can
    surface a specific, actionable refusal rather than a generic parse
    error."""
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is a bare number, not a unit-tagged string - e.g. \"{value}mm\"")
    match = _DIMENSION_RE.match(value)
    if match is None:
        if re.match(r"^\s*-?\d+(\.\d+)?\s*$", value):
            raise ValueError(f"{value!r} is a bare number with no unit - e.g. \"{value}mm\" or \"{value}in\"")
        raise ValueError(f"{value!r} isn't a recognized \"<number><unit>\" dimension")
    number, unit = match.groups()
    return float(number) * UNIT_TO_MM[unit.lower()]


def validate_dimensions(dimensions: dict[str, str]) -> tuple[dict[str, float], list[str]]:
    """Converts every dimension to mm. Returns (converted, errors) - errors
    is empty on full success; any entry that fails to parse is omitted from
    `converted` and named in `errors` instead of raising, so the caller can
    report every problem dimension in one refusal instead of one at a time."""
    converted: dict[str, float] = {}
    errors: list[str] = []
    for name, value in dimensions.items():
        try:
            converted[name] = parse_dimension_mm(value)
        except ValueError as exc:
            errors.append(f"{name}: {exc}")
    return converted, errors
