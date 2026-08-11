"""export_step (PROMPTS.md A14) - export any part already in cad/generated/
or cad/verified/ to STEP, on demand, without re-running the generation
loop. cad_generate already exports STEP+STL automatically on success -
this is for re-exporting later, or exporting a hand-verified cad/verified/
part cad_generate never touched.
"""

from tools._cad_export_common import build_default_solid, find_part_script

REQUIRES_CONFIRMATION = False


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "export_step",
            "description": "Export a part from cad/generated/ or cad/verified/ to a STEP file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "the part's name (its directory under cad/generated/ or cad/verified/)"},
                },
                "required": ["name"],
            },
        },
    }


async def execute(name: str) -> str:
    try:
        script_path = find_part_script(name)
        result = build_default_solid(script_path)
    except Exception as exc:
        return f"Could not export {name!r}: {exc}"

    out_path = script_path.parent / f"{name}.step"
    result.val().exportStep(str(out_path))
    return f"Exported {name!r} to {out_path}."
