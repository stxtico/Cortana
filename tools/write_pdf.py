"""write_pdf (PROMPTS.md A24) - write-capable, gated the same way
tools/write_file.py already is: REQUIRES_CONFIRMATION = True, whitelisted to
[tools].write_whitelist_dirs via tools/_fs.py.

Uses reportlab's platypus layer (SimpleDocTemplate + Paragraph), not raw
canvas.drawString() calls - canvas draws text at a fixed position with no
wrapping or pagination, which would silently truncate or overlap anything
longer than one line. Paragraph content is XML-escaped before being handed
to reportlab (which parses a small HTML-like markup subset for formatting) -
without that, real content containing '&', '<', or '>' would either raise
or render wrong.
"""

from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from tools import _fs

REQUIRES_CONFIRMATION = True

_WRITE_KEY = "write_whitelist_dirs"


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "write_pdf",
            "description": (
                "Create a PDF with an optional title and body text (paragraphs separated by "
                "blank lines). Only these directories (and their subdirectories) are "
                f"write-authorized: {_fs.whitelist_description(_WRITE_KEY)}. Requires user "
                "confirmation before it actually runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the .pdf file, absolute or relative to the project root."},
                    "content": {"type": "string", "description": "Body text. Separate paragraphs with a blank line."},
                    "title": {"type": "string", "description": "Optional title at the top of the document."},
                },
                "required": ["path", "content"],
            },
        },
    }


def describe(path: str, content: str, title: str | None = None) -> str:
    preview = content if len(content) <= 120 else content[:120] + "…"
    heading = f" (titled {title!r})" if title else ""
    return f"Write a PDF{heading} to {path!r}:\n    {preview!r}"


async def execute(path: str, content: str, title: str | None = None) -> str:
    resolved = _fs.resolve_in_whitelist(path, _WRITE_KEY)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    story = []
    if title:
        story.append(Paragraph(escape(title), styles["Title"]))
        story.append(Spacer(1, 12))
    for paragraph in content.split("\n\n"):
        safe = escape(paragraph).replace("\n", "<br/>")
        story.append(Paragraph(safe, styles["Normal"]))
        story.append(Spacer(1, 12))

    doc = SimpleDocTemplate(str(resolved), pagesize=letter)
    doc.build(story)

    return f"Wrote a PDF to {resolved}."
