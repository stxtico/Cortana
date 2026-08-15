"""write_xlsx (PROMPTS.md A24) - write-capable, gated the same way
tools/write_file.py already is: REQUIRES_CONFIRMATION = True, whitelisted to
[tools].write_whitelist_dirs via tools/_fs.py.
"""

import openpyxl

from tools import _fs

REQUIRES_CONFIRMATION = True

_WRITE_KEY = "write_whitelist_dirs"


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "write_xlsx",
            "description": (
                "Create a spreadsheet (.xlsx) from tabular data - a list of rows, each row a "
                "list of cell values. The first row is typically the header. Only these "
                f"directories (and their subdirectories) are write-authorized: "
                f"{_fs.whitelist_description(_WRITE_KEY)}. Requires user confirmation before "
                "it actually runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the .xlsx file, absolute or relative to the project root."},
                    "rows": {
                        "type": "array",
                        "items": {"type": "array", "items": {}},
                        "description": "Rows of cell values, e.g. [[\"Name\", \"Total\"], [\"Widget\", 42]].",
                    },
                    "sheet_name": {"type": "string", "description": "Optional sheet name (default 'Sheet1')."},
                },
                "required": ["path", "rows"],
            },
        },
    }


def describe(path: str, rows: list, sheet_name: str = "Sheet1") -> str:
    return f"Write a spreadsheet ({len(rows)} rows, sheet {sheet_name!r}) to {path!r}."


async def execute(path: str, rows: list, sheet_name: str = "Sheet1") -> str:
    resolved = _fs.resolve_in_whitelist(path, _WRITE_KEY)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    for row in rows:
        ws.append(row)
    wb.save(str(resolved))

    return f"Wrote a spreadsheet ({len(rows)} rows) to {resolved}."
