"""transcribe_media (PROMPTS.md A23) - points the same Whisper wrapper
services/ears/pipeline.py uses at an arbitrary file instead of the live
mic. Lazy, module-level singleton Transcriber (CLAUDE.md rule 7: one
persistent model instance per process, loaded once, not per call) - not a
literal shared object with the live ears pipeline, which is architecturally
a separate process (services/ears/pipeline.py's own Transcriber only ever
exists inside that process); this module loads its OWN instance, with the
exact same [audio.stt] config (model/device/compute_type/language), the
first time this tool is actually called in whatever process it runs in,
then reuses it for that process's remaining lifetime.

Only whitelisted-directory files are accepted (tools/_fs.py, the same
whitelist read_file/list_dir enforce) - a real path-resolution and
existence check happens before ever touching the GPU-resident model.
"""

import tomllib
from pathlib import Path

from services.ears.stt import Transcriber
from tools import _fs

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

REQUIRES_CONFIRMATION = False

_transcriber: Transcriber | None = None


def _get_transcriber() -> Transcriber:
    global _transcriber
    if _transcriber is None:
        with CONFIG_PATH.open("rb") as f:
            stt_cfg = tomllib.load(f)["audio"]["stt"]
        _transcriber = Transcriber(
            model_name=stt_cfg["model"],
            device=stt_cfg["device"],
            compute_type=stt_cfg["compute_type"],
            language=stt_cfg["language"],
        )
    return _transcriber


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "transcribe_media",
            "description": (
                "Transcribe an audio or video file to text using the same Whisper model "
                "used for live conversation. Only files inside these directories (and their "
                f"subdirectories) are accessible: {_fs.whitelist_description()}. Use for "
                "summarizing a recording, or answering questions about what was said in a "
                "meeting or video file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the audio/video file, absolute or relative to the project root."},
                },
                "required": ["path"],
            },
        },
    }


async def execute(path: str) -> str:
    resolved = _fs.resolve_in_whitelist(path)
    if not resolved.exists():
        return f"No such file: {resolved}"
    if not resolved.is_file():
        return f"Not a file: {resolved}"
    transcriber = _get_transcriber()
    result = transcriber.transcribe_file(str(resolved))
    if not result.text:
        return "(no speech detected in this file)"
    return result.text
