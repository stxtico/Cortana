"""process_list (PROMPTS.md A23) - running processes with CPU and memory,
via psutil (already a direct dependency since A21's workers).

psutil.Process.cpu_percent() returns a meaningless 0.0 on its first call per
process (it measures elapsed CPU time since the *previous* call, and there
is no previous call yet) - a real, well-documented psutil behavior, not an
edge case worth skipping. Every process is "primed" with one throwaway call
first, then re-measured after a real, short, configurable wait
([tools.process_list].sample_interval_s), so the numbers returned are an
actual sampled rate, not a uniformly misleading 0.0 across the board.
"""

import asyncio
import tomllib
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

REQUIRES_CONFIRMATION = False


def _sample_interval_s() -> float:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config.get("tools", {}).get("process_list", {}).get("sample_interval_s", 0.3)


def spec() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "process_list",
            "description": "List currently running processes with their CPU and memory usage, highest usage first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sort_by": {"type": "string", "enum": ["cpu", "memory"], "description": "Sort by CPU percent or memory usage. Default 'cpu'."},
                    "limit": {"type": "integer", "description": "Maximum number of processes to return. Default 15."},
                },
                "required": [],
            },
        },
    }


async def execute(sort_by: str = "cpu", limit: int = 15) -> str:
    procs = list(psutil.process_iter(["pid", "name"]))
    for p in procs:
        try:
            p.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    await asyncio.sleep(_sample_interval_s())

    rows = []
    for p in procs:
        try:
            cpu = p.cpu_percent(None)
            mem_mb = p.memory_info().rss / (1024 * 1024)
            rows.append((p.info["name"] or f"pid {p.pid}", p.pid, cpu, mem_mb))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    rows.sort(key=lambda r: r[3] if sort_by == "memory" else r[2], reverse=True)
    rows = rows[: max(1, limit)]

    if not rows:
        return "No processes found."
    return "\n".join(f"{name} (pid {pid}): {cpu:.1f}% CPU, {mem:.0f} MB" for name, pid, cpu, mem in rows)
