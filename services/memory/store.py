"""SQLite + sqlite-vec passage store - the retrieval layer's durable "everything
ever said." Synchronous by design (sqlite3 is synchronous); services/memory/
manager.py is the only caller and wraps every call in asyncio.to_thread so the
event loop never blocks on disk I/O.

One persistent connection per process (rule 7), guarded by a lock: sqlite3
connections aren't safe to share across threads without check_same_thread=False,
and asyncio.to_thread can hand consecutive calls to different worker threads -
same reasoning as XTTSEngine._model_lock (services/voice/xtts_engine.py).
"""

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


@dataclass
class Passage:
    id: int
    session_id: str
    timestamp: str
    role: str
    source: str  # "raw" or "summary"
    text: str
    distance: float | None = None


def _connect(db_path: Path, embedding_dim: int) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS passages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            role TEXT NOT NULL,
            source TEXT NOT NULL,
            text TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS passages_vec USING vec0(
            embedding float[{embedding_dim}]
        )
        """
    )
    conn.commit()
    return conn


def get_connection(db_path: Path, embedding_dim: int) -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect(db_path, embedding_dim)
    return _conn


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def write_passage(
    conn: sqlite3.Connection, session_id: str, role: str, source: str, text: str, embedding: list[float]
) -> int:
    with _lock:
        cur = conn.execute(
            "INSERT INTO passages (session_id, timestamp, role, source, text) VALUES (?, ?, ?, ?, ?)",
            (session_id, datetime.now(timezone.utc).isoformat(), role, source, text),
        )
        passage_id = cur.lastrowid
        conn.execute(
            "INSERT INTO passages_vec (rowid, embedding) VALUES (?, ?)",
            (passage_id, sqlite_vec.serialize_float32(embedding)),
        )
        conn.commit()
        return passage_id


def query_similar(conn: sqlite3.Connection, embedding: list[float], top_k: int) -> list[Passage]:
    with _lock:
        rows = conn.execute(
            """
            SELECT p.id, p.session_id, p.timestamp, p.role, p.source, p.text, v.distance
            FROM passages_vec v
            JOIN passages p ON p.id = v.rowid
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance
            """,
            (sqlite_vec.serialize_float32(embedding), top_k),
        ).fetchall()
    return [Passage(*row) for row in rows]


def list_all(conn: sqlite3.Connection, session_id: str | None = None, limit: int = 200) -> list[Passage]:
    with _lock:
        if session_id:
            rows = conn.execute(
                "SELECT id, session_id, timestamp, role, source, text FROM passages "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, session_id, timestamp, role, source, text FROM passages ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [Passage(*row) for row in rows]


def list_sessions(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """(session_id, earliest timestamp, message count), most recent session first."""
    with _lock:
        rows = conn.execute(
            "SELECT session_id, MIN(timestamp), COUNT(*) FROM passages GROUP BY session_id ORDER BY MIN(timestamp) DESC"
        ).fetchall()
    return rows


def delete_passage(conn: sqlite3.Connection, passage_id: int) -> bool:
    with _lock:
        cur = conn.execute("DELETE FROM passages WHERE id = ?", (passage_id,))
        conn.execute("DELETE FROM passages_vec WHERE rowid = ?", (passage_id,))
        conn.commit()
        return cur.rowcount > 0


def update_passage(conn: sqlite3.Connection, passage_id: int, text: str, embedding: list[float] | None) -> bool:
    """Corrects a stored entry's text (PROMPTS.md A7/A12 - the inspector was
    built for drift correction specifically, so this has to actually work, not
    just view). Re-embeds when given a vector so retrieval matches what's
    actually stored now, not a stale vector for text that's since been
    corrected - callers that skip re-embedding (embedding=None) get a faster
    edit at the cost of retrieval still matching the old text until the next
    one that does re-embed."""
    with _lock:
        cur = conn.execute("UPDATE passages SET text = ? WHERE id = ?", (text, passage_id))
        if cur.rowcount == 0:
            conn.rollback()
            return False
        if embedding is not None:
            conn.execute("DELETE FROM passages_vec WHERE rowid = ?", (passage_id,))
            conn.execute(
                "INSERT INTO passages_vec (rowid, embedding) VALUES (?, ?)",
                (passage_id, sqlite_vec.serialize_float32(embedding)),
            )
        conn.commit()
        return True
