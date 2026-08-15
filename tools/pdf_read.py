"""pdf_read (PROMPTS.md A24) - extracts text and tables from a PDF, read-only,
whitelisted to [tools].whitelist_dirs (tools/_fs.py) same as read_file/list_dir.
No REQUIRES_CONFIRMATION - a pure read (CLAUDE.md rule 4).

Uses pdfplumber rather than a raw text-layer extractor because it also does
table detection (page.extract_tables()) - PROMPTS.md asks for "text and
tables" explicitly, and a table flattened through plain text extraction reads
as a run of misaligned words, not rows.
"""

import pdfplumber

from tools import _fs


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "pdf_read",
            "description": (
                "Extract text and tables from a PDF file. Only files inside these "
                f"directories (and their subdirectories) are accessible: {_fs.whitelist_description()}. "
                "Use list_dir first if you don't already know the exact filename."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the PDF file, absolute or relative to the project root."},
                    "max_pages": {"type": "integer", "description": "Optional cap on how many pages to read from the start (default: all)."},
                },
                "required": ["path"],
            },
        },
    }


def _format_table(table: list[list]) -> str:
    rows = [" | ".join("" if cell is None else str(cell) for cell in row) for row in table]
    return "\n".join(rows)


async def execute(path: str, max_pages: int | None = None) -> str:
    resolved = _fs.resolve_in_whitelist(path)
    if not resolved.exists():
        return f"No such file: {resolved}"
    if not resolved.is_file():
        return f"Not a file: {resolved}"

    sections = []
    with pdfplumber.open(str(resolved)) as pdf:
        pages = pdf.pages[:max_pages] if max_pages else pdf.pages
        for i, page in enumerate(pages, start=1):
            text = (page.extract_text() or "").strip()
            block = [f"--- Page {i} ---"]
            block.append(text if text else "(no extractable text on this page)")
            for j, table in enumerate(page.extract_tables(), start=1):
                block.append(f"[Table {j} on page {i}]")
                block.append(_format_table(table))
            sections.append("\n".join(block))

    if not sections:
        return f"{resolved} has no pages."
    return "\n\n".join(sections)
