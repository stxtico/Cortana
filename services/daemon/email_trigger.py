"""email_trigger - A11's second dormant trigger source (PROMPTS.md), same
reasoning as calendar_trigger.py: real code, honestly gated by
tools/_outlook.py's is_available() (rule 10 - never launches Outlook itself),
not verified live since there's nothing here to test against.

"A config rule" is a simple substring match against subject/sender, not a
rules engine - [daemon.email].subject_contains/from_contains in cortana.toml,
both empty by default so nothing matches (and nothing surfaces) until
explicitly configured, same "no directory write-authorized until you add
one" spirit as A9's write_file whitelist.
"""

import tomllib
from pathlib import Path

from tools import _outlook

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

_OL_FOLDER_INBOX = 6


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("daemon", {}).get("email", {})


def _matches_rule(subject: str, sender: str, config: dict) -> bool:
    subject_rules = config.get("subject_contains", [])
    from_rules = config.get("from_contains", [])
    subject_l, sender_l = subject.lower(), sender.lower()
    return any(r.lower() in subject_l for r in subject_rules) or any(r.lower() in sender_l for r in from_rules)


async def is_available() -> bool:
    return await _outlook.is_available()


async def poll() -> list[dict]:
    """Not verified live - dormant on this machine, nothing here to test
    against (see module docstring). Only looks at unread mail so a restart
    doesn't re-surface the whole inbox history against the rules."""
    config = _load_config()
    namespace = _outlook.get_namespace()
    inbox = namespace.GetDefaultFolder(_OL_FOLDER_INBOX)
    items = inbox.Items.Restrict("[Unread] = true")

    candidates = []
    for item in items:
        if _matches_rule(item.Subject, item.SenderName, config):
            candidates.append({
                "source": "email",
                "summary": f"Email from {item.SenderName} matching a configured rule.",
                "detail": f"Subject: {item.Subject!r}.",
                "id": f"email-{item.EntryID}",
            })
    return candidates
