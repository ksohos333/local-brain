"""RAG memory backend using sqlite-vec + FTS5.

Replaces the previous Postgres + pgvector implementation. All data lives in a
single SQLite file at ~/.local/share/local-brain/brain.db. Vectors are stored
via the sqlite-vec extension; full-text search uses SQLite FTS5.
"""

import json
import re
import sqlite3
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import sqlite_vec

from local_brain import config, embeddings


DB_PATH = Path.home() / ".local" / "share" / "local-brain" / "brain.db"
DEFAULT_DIMENSION = 384


def _vector_dimension() -> int:
    """Return the configured embedding dimension."""
    dim = config.get("embedding.dimension")
    if dim:
        return int(dim)
    return DEFAULT_DIMENSION


def _ensure_db_dir() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _connect():
    """Open a SQLite connection with sqlite-vec loaded."""
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.row_factory = sqlite3.Row
    # WAL mode gives better concurrency for single-writer / multi-reader.
    conn.execute("PRAGMA journal_mode=WAL")
    # Wait rather than raise SQLITE_BUSY when a bulk reindex or an FTS
    # optimize holds the write lock.
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _pack_vector(vector: List[float]) -> bytes:
    """Pack a float vector into little-endian float32 bytes for sqlite-vec."""
    return struct.pack(f"<{len(vector)}f", *vector)


def _parse_vector(vec_bytes: bytes) -> List[float]:
    """Unpack sqlite-vec float32 bytes back into a Python list."""
    if not vec_bytes:
        return []
    count = len(vec_bytes) // 4
    return list(struct.unpack(f"<{count}f", vec_bytes))


def _to_fts_text(content: str) -> str:
    """Normalize content for FTS5 indexing (strip code blocks and markup)."""
    text = re.sub(r"```[\s\S]*?```", " ", content)
    text = re.sub(r"`[^`]+`", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def _chunks_to_index(content: str, max_chars: int = 1500) -> List[str]:
    """Split content into chunks small enough to embed accurately."""
    parts = re.split(r"\n(?=##\s+)", content)
    chunks = []
    for part in parts:
        part = part.strip()
        if len(part) < 30:
            continue
        if len(part) <= max_chars:
            chunks.append(part)
            continue
        paragraphs = part.split("\n\n")
        current = ""
        for para in paragraphs:
            if len(current) + len(para) > max_chars:
                if current:
                    chunks.append(current.strip())
                current = para
            else:
                current += "\n\n" + para if current else para
        if current:
            chunks.append(current.strip())
    return chunks


def _extract_title_from_text(text: str) -> Optional[str]:
    """Extract first H1 or return None."""
    m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_schema() -> None:
    """Create RAG tables and indexes (idempotent)."""
    dim = _vector_dimension()
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_id TEXT,
                session_id TEXT,
                role TEXT,
                content TEXT NOT NULL,
                metadata TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_chunks_source ON memory_chunks(source, session_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_chunks_created ON memory_chunks(created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_chunks_session ON memory_chunks(session_id, created_at DESC)"
        )
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_vec USING vec0(
                embedding float[{dim}]
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts USING fts5(
                content,
                content='memory_chunks',
                content_rowid='id'
            )
            """
        )
        conn.commit()


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert a SQLite row to the public dict format."""
    return {
        "id": row["id"],
        "source": row["source"],
        "source_id": row["source_id"],
        "session_id": row["session_id"],
        "role": row["role"],
        "content": row["content"],
        "metadata": json.loads(row["metadata"] or "{}"),
        "created_at": row["created_at"],
        "vector_score": 0.0,
        "text_rank": 0.0,
    }


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def index_chunk(
    source: str,
    content: str,
    source_id: Optional[str] = None,
    session_id: Optional[str] = None,
    role: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    created_at: Optional[datetime] = None,
) -> int:
    """Index a single text chunk into the memory store."""
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    created_iso = created_at.isoformat() if created_at else None

    vector = embeddings.embed([content])[0]
    vec_bytes = _pack_vector(vector)

    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO memory_chunks
            (source, source_id, session_id, role, content, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                source_id,
                session_id,
                role,
                content,
                json.dumps(metadata or {}),
                created_iso,
            ),
        )
        chunk_id = cur.lastrowid
        conn.execute(
            "INSERT INTO memory_chunks_vec(rowid, embedding) VALUES (?, ?)",
            (chunk_id, vec_bytes),
        )
        conn.execute(
            "INSERT INTO memory_chunks_fts(rowid, content) VALUES (?, ?)",
            (chunk_id, _to_fts_text(content)),
        )
        conn.commit()
    return chunk_id


def index_note(path: str, title: str, content: str) -> int:
    """Index a markdown note, chunked by headings."""
    count = 0
    for chunk in _chunks_to_index(content):
        if chunk.startswith("# "):
            chunk_text = chunk
        else:
            chunk_text = f"# {title}\n\n{chunk}"
        index_chunk(
            source="local-brain",
            source_id=path,
            content=chunk_text,
            metadata={"path": path, "title": title},
        )
        count += 1
    return count


def index_vault_directory(root: Path, source: str = "local-brain") -> int:
    """Index all markdown files under a directory tree."""
    count = 0
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root).as_posix()
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
            title = _extract_title_from_text(content) or p.stem
            for chunk in _chunks_to_index(content):
                if chunk.startswith("# "):
                    chunk_text = chunk
                else:
                    chunk_text = f"# {title}\n\n{chunk}"
                index_chunk(
                    source=source,
                    source_id=rel,
                    content=chunk_text,
                    metadata={"path": rel, "title": title, "vault": root.name},
                )
                count += 1
        except Exception:
            continue
    return count


def index_turn(
    session_id: str,
    role: str,
    content: str,
    source_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> int:
    """Index a single chat turn."""
    return index_chunk(
        source="bigus_dickus",
        source_id=source_id,
        session_id=session_id,
        role=role,
        content=content,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _vector_search(query: str, k: int) -> List[Dict[str, Any]]:
    """Search using vector similarity."""
    try:
        query_vector = embeddings.embed([query])[0]
        vec_bytes = _pack_vector(query_vector)
    except Exception:
        return []

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                memory_chunks.id,
                memory_chunks.source,
                memory_chunks.source_id,
                memory_chunks.session_id,
                memory_chunks.role,
                memory_chunks.content,
                memory_chunks.metadata,
                memory_chunks.created_at,
                vec.distance
            FROM memory_chunks_vec AS vec
            JOIN memory_chunks ON memory_chunks.id = vec.rowid
            WHERE vec.embedding MATCH ? AND vec.k = ?
            ORDER BY vec.distance
            """,
            (vec_bytes, k),
        ).fetchall()

    results = []
    for row in rows:
        d = _row_to_dict(row)
        distance = row["distance"]
        d["vector_score"] = max(0.0, 1.0 - float(distance)) if distance is not None else 0.0
        d["text_rank"] = 0.0
        results.append(d)
    return results


def _text_search(query: str, k: int) -> List[Dict[str, Any]]:
    """Search using FTS5 full-text search."""
    # Sanitize user-provided tokens for FTS5: hyphens would otherwise be parsed
    # as the unary NOT operator and can trigger "no such column" errors. We keep
    # the query as a simple bag-of-words AND search, which matches user intent.
    safe_query = re.sub(r"[^\w\s]", " ", query)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                memory_chunks.id,
                memory_chunks.source,
                memory_chunks.source_id,
                memory_chunks.session_id,
                memory_chunks.role,
                memory_chunks.content,
                memory_chunks.metadata,
                memory_chunks.created_at,
                bm25(memory_chunks_fts) AS rank
            FROM memory_chunks_fts
            JOIN memory_chunks ON memory_chunks.id = memory_chunks_fts.rowid
            WHERE memory_chunks_fts MATCH ?
            ORDER BY bm25(memory_chunks_fts)
            LIMIT ?
            """,
            (safe_query, k),
        ).fetchall()

    results = []
    for row in rows:
        d = _row_to_dict(row)
        rank = row["rank"]
        # FTS5 bm25() is unbounded and lower is better; it can be negative.
        # Invert and clip so larger = better, then normalize later.
        d["text_rank"] = max(0.0, -float(rank)) if rank is not None else 0.0
        d["vector_score"] = 0.0
        results.append(d)
    return results


def search(query: str, k: int = 5, current_session: Optional[str] = None) -> List[Dict[str, Any]]:
    """Hybrid search: combine FTS5 rank and vector similarity."""
    text_results = _text_search(query, k * 2)
    vector_results = _vector_search(query, k * 2)

    by_id = {}
    for r in text_results:
        by_id[r["id"]] = r
    for r in vector_results:
        existing = by_id.get(r["id"])
        if existing:
            existing["vector_score"] = r["vector_score"]
        else:
            by_id[r["id"]] = r

    max_text = max((r["text_rank"] for r in by_id.values() if r["text_rank"] > 0), default=1.0)
    for r in by_id.values():
        text_norm = r["text_rank"] / max_text if max_text > 0 else 0.0
        vector_norm = r["vector_score"]  # already 0..1
        r["score"] = 0.6 * text_norm + 0.4 * vector_norm

    results = sorted(by_id.values(), key=lambda x: x["score"], reverse=True)[:k]
    return results


# ---------------------------------------------------------------------------
# Recent context
# ---------------------------------------------------------------------------

def recent_context(session_id: str, n: int = 10) -> List[Dict[str, Any]]:
    """Get recent turns from a specific session."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, source, source_id, session_id, role, content, metadata, created_at
            FROM memory_chunks
            WHERE session_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (session_id, n),
        ).fetchall()

    results = []
    for row in reversed(rows):
        results.append(_row_to_dict(row))
    return results


# ---------------------------------------------------------------------------
# Deletion / stats
# ---------------------------------------------------------------------------

def _purge_chunks(conn: sqlite3.Connection, rows: List[sqlite3.Row]) -> int:
    """Remove chunk rows from the content table, the vec table and the FTS index.

    All three stores must be hit explicitly: memory_chunks_fts is an
    external-content FTS5 table, so deleting the content row alone leaves the
    posting behind and it keeps showing up in searches.
    """
    for row in rows:
        chunk_id = row["id"]
        conn.execute("DELETE FROM memory_chunks_vec WHERE rowid = ?", (chunk_id,))
        # An external-content FTS5 table must be handed the ORIGINAL text
        # it indexed, otherwise it cannot know which terms to drop.
        # Passing NULL here removes nothing, so every delete/insert cycle
        # leaked the whole deleted chunk into the index forever and grew
        # memory_chunks_fts_data without bound.
        conn.execute(
            "INSERT INTO memory_chunks_fts(memory_chunks_fts, rowid, content) "
            "VALUES ('delete', ?, ?)",
            (chunk_id, row["content"]),
        )
        conn.execute("DELETE FROM memory_chunks WHERE id = ?", (chunk_id,))
    return len(rows)


def delete_source(source: str, source_id: Optional[str] = None) -> int:
    """Delete chunks by source (and optionally source_id)."""
    with _connect() as conn:
        if source_id:
            rows = conn.execute(
                "SELECT id, content FROM memory_chunks WHERE source = ? AND source_id = ?",
                (source, source_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, content FROM memory_chunks WHERE source = ?",
                (source,),
            ).fetchall()

        removed = _purge_chunks(conn, rows)
        conn.commit()
        return removed


def delete_session(session_id: str) -> int:
    """Delete every chunk belonging to one chat session.

    Used when a chat is permanently deleted: the session's turns live here as
    `bigus_dickus` chunks keyed by session_id, and leaving them behind would
    keep the deleted conversation retrievable through /api/rag/search.
    Returns the number of chunks removed.
    """
    if not session_id:
        return 0
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, content FROM memory_chunks WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        removed = _purge_chunks(conn, rows)
        conn.commit()
    if removed:
        # Each purge appends a fresh FTS5 segment; compact so repeated deletes
        # do not grow the shadow tables without bound.
        optimize_index()
    return removed


def rebuild_index() -> bool:
    """Discard and recreate the whole FTS5 index from the content table.

    'optimize' only merges existing segments, so it cannot repair an index
    that already contains stale postings. 'rebuild' re-derives every posting
    from memory_chunks, which is the only cure for accumulated corruption.
    Returns True on success.
    """
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO memory_chunks_fts(memory_chunks_fts) VALUES('rebuild')"
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        print(f"[rag] rebuild_index failed: {exc}", file=sys.stderr)
        return False
    finally:
        conn.close()


def optimize_index() -> bool:
    """Compact the FTS5 index and truncate the WAL.

    Each bulk delete+insert cycle (see delete_source) appends new FTS5
    segments. Nothing reclaims them, so the shadow tables grow without bound
    even though the underlying rows do not. An explicit 'optimize' merges
    those segments. Returns True on success.
    """
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO memory_chunks_fts(memory_chunks_fts) VALUES('optimize')"
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return True
    except sqlite3.Error as exc:
        print(f"[rag] optimize_index failed: {exc}", file=sys.stderr)
        return False
    finally:
        conn.close()


def stats() -> Dict[str, Any]:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
        embedded = conn.execute(
            "SELECT COUNT(*) FROM memory_chunks_vec"
        ).fetchone()[0]
        by_source = {
            row["source"]: row["c"]
            for row in conn.execute(
                "SELECT source, COUNT(*) AS c FROM memory_chunks GROUP BY source"
            ).fetchall()
        }
    return {"total": total, "embedded": embedded, "by_source": by_source}


# ---------------------------------------------------------------------------
# Import utilities (for migrations)
# ---------------------------------------------------------------------------

def import_chunk(
    chunk_id: int,
    source: str,
    content: str,
    source_id: Optional[str] = None,
    session_id: Optional[str] = None,
    role: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    created_at: Optional[str] = None,
    vector: Optional[List[float]] = None,
) -> None:
    """Import a chunk with an explicit id and optional pre-computed vector.

    Used by the Postgres -> SQLite migration script.
    """
    if vector:
        vec_bytes = _pack_vector(vector)
    else:
        try:
            vec = embeddings.embed([content])[0]
            vec_bytes = _pack_vector(vec)
        except Exception:
            vec_bytes = None

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO memory_chunks
            (id, source, source_id, session_id, role, content, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk_id,
                source,
                source_id,
                session_id,
                role,
                content,
                json.dumps(metadata or {}),
                created_at,
            ),
        )
        if vec_bytes:
            conn.execute(
                "INSERT INTO memory_chunks_vec(rowid, embedding) VALUES (?, ?)",
                (chunk_id, vec_bytes),
            )
        conn.execute(
            "INSERT INTO memory_chunks_fts(rowid, content) VALUES (?, ?)",
            (chunk_id, _to_fts_text(content)),
        )
        conn.commit()
