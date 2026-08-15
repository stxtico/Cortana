"""email_draft (PROMPTS.md A24) - composes an Outlook email and leaves it in
Drafts. Never sends - there is no send path in this module at all, not a
flag that defaults off; MailItem.Save() (not .Send()) is the only COM call
that touches the message. The safe default this tool exists to cover, not a
stepping stone toward a future send capability.

Same dormancy as calendar_read.py/email_read.py and the same reason
(tools/_outlook.py): Outlook has to already be running, checked without ever
launching it. Reuses _outlook.get_application() (new - see that module) for
Application.CreateItem(), since MailItem creation lives on the Application
object, not the namespace calendar_read/email_read already use.

Write-capable (creates a real, persistent draft in the user's mailbox) -
REQUIRES_CONFIRMATION = True, same gating as every other write tool
(CLAUDE.md rule 4).
"""

from tools import _outlook

REQUIRES_CONFIRMATION = True

_OL_MAIL_ITEM = 0


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "email_draft",
            "description": (
                "Compose an email and save it to the Outlook Drafts folder. Never sends - "
                "the draft is left for the user to review and send themselves. Requires "
                "Outlook to already be running."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address(es), semicolon-separated for multiple."},
                    "subject": {"type": "string", "description": "Email subject line."},
                    "body": {"type": "string", "description": "Plain-text email body."},
                },
                "required": ["to", "subject", "body"],
            },
        },
    }


async def is_available() -> bool:
    return await _outlook.is_available()


def describe(to: str, subject: str, body: str) -> str:
    preview = body if len(body) <= 120 else body[:120] + "…"
    return f"Save a draft email to {to!r}, subject {subject!r}:\n    {preview!r}\n    (saved to Drafts, never sent)"


async def execute(to: str, subject: str, body: str) -> str:
    outlook = _outlook.get_application()
    mail = outlook.CreateItem(_OL_MAIL_ITEM)
    mail.To = to
    mail.Subject = subject
    mail.Body = body
    mail.Save()
    return f"Saved a draft to {to!r} (subject: {subject!r}) in Outlook's Drafts folder. Not sent."
