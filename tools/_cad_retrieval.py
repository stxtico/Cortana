"""Retrieval over cad/verified/ (PROMPTS.md A14 - "retrieve the 3 most
similar parts... as few-shot examples on every generation"). Reuses
services/memory/embeddings.py's embed() - the same local nomic-embed-text
client A6/A7's conversation retrieval already uses - rather than a second
embedding path. Not backed by the sqlite-vec store services/memory/store.py
owns (that schema is conversation passages, not CAD parts, and cad/verified/
is small enough - a personal parts library, not a corpus - that embedding
every description live on each call is cheap and needs no cache/invalidation
story)."""

import json
from pathlib import Path

from services.memory import embeddings

ROOT = Path(__file__).resolve().parent.parent
VERIFIED_ROOT = ROOT / "cad" / "verified"


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _list_parts() -> list[dict]:
    parts = []
    if not VERIFIED_ROOT.exists():
        return parts
    for part_dir in sorted(VERIFIED_ROOT.iterdir()):
        if not part_dir.is_dir():
            continue
        desc_path = part_dir / "description.md"
        script_path = part_dir / "part.py"
        meta_path = part_dir / "meta.json"
        if not (desc_path.exists() and script_path.exists()):
            continue
        description = desc_path.read_text(encoding="utf-8").strip()
        if not description:
            continue  # nothing to embed or show as a few-shot example
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        parts.append({
            "name": part_dir.name,
            "description": description,
            "script": script_path.read_text(encoding="utf-8"),
            "meta": meta,
        })
    return parts


async def retrieve_similar_parts(query_description: str, top_k: int = 3) -> list[dict]:
    """Returns up to top_k parts from cad/verified/, ranked by cosine
    similarity between the query description and each part's
    description.md. Empty list if cad/verified/ has nothing usable yet -
    that's a real, expected state early on, not an error."""
    parts = _list_parts()
    if not parts:
        return []
    query_vec = await embeddings.embed(query_description)
    for part in parts:
        part["_vec"] = await embeddings.embed(part["description"])
    ranked = sorted(parts, key=lambda p: _cosine(query_vec, p["_vec"]), reverse=True)
    for part in ranked:
        del part["_vec"]
    return ranked[:top_k]
