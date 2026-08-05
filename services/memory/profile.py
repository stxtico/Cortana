"""config/profile.md, read verbatim. Durable, hand-edited, injected into every
turn - never retrieved, never written to by the app itself (that's exactly the
memory-drift failure scripts/memory.py's inspector exists to guard against)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PROFILE_PATH = ROOT / "config" / "profile.md"


def load_profile() -> str:
    return PROFILE_PATH.read_text(encoding="utf-8") if PROFILE_PATH.exists() else ""
