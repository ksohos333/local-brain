"""Ingest opencode chats into the RAG store.

local-brain's own /api/chat was the only thing that ever wrote to memory_chunks,
so the store accumulated embeddings for a chat tool that no longer exists
(59 orphaned sessions in the old id format) while every real opencode chat went
unindexed. This closes that gap.

Two rules matter here:

* A chat marked private in the sidecar registry is never indexed, and if it was
  indexed before being marked it gets purged, so marking a chat private is
  retroactive rather than only forward-looking.
* Chats are only re-embedded when their updated timestamp actually moved, so a
  routine sync costs one cheap comparison per chat.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from local_brain import config, rag

SOURCE = "opencode"
OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
PRIVACY_FILE = Path.home() / ".config" / "opencode" / "opencode-chats.json"
MIN_CHUNK_CHARS = 20
MAX_CHUNK_CHARS = 1500


def private_sessions() -> set[str]:
    """Session ids the user marked private. Read from the shared sidecar file."""
    try:
        data = json.loads(PRIVACY_FILE.read_text())
    except Exception:
        return set()
    ids = data.get("private_sessions", []) if isinstance(data, dict) else []
    return {str(x) for x in ids} if isinstance(ids, list) else set()


def _connect_readonly() -> sqlite3.Connection | None:
    if not OPENCODE_DB.exists():
        return None
    conn = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _sessions_table(conn: sqlite3.Connection) -> str:
    for name in ("session", "session_v2"):
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone():
            return name
    raise RuntimeError("opencode db exposes no session table")


def _split(text: str) -> list[str]:
    """Break a long turn into embeddable pieces, dropping slivers."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    pieces, current = [], ""
    for para in text.split("\n\n"):
        if current and len(current) + len(para) + 2 > MAX_CHUNK_CHARS:
            pieces.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        pieces.append(current)
    return [p for p in pieces if len(p.strip()) >= MIN_CHUNK_CHARS]


def _turns_for_session(conn: sqlite3.Connection, session_id: str) -> list[tuple[str, str]]:
    """Pull the human-readable turns of one chat, in order.

    Only `text` parts are kept. Tool calls, reasoning traces and step markers
    make up the bulk of the row count but are noise for recall, and indexing
    them would swamp the store with implementation detail.
    """
    rows = conn.execute(
        """
        SELECT json_extract(m.data, '$.role') AS role,
               json_extract(p.data, '$.text') AS text
        FROM part p
        JOIN message m ON m.id = p.message_id
        WHERE p.session_id = ?
          AND json_extract(p.data, '$.type') = 'text'
          AND json_extract(p.data, '$.text') IS NOT NULL
        ORDER BY p.rowid
        """,
        (session_id,),
    ).fetchall()
    return [(r["role"] or "user", r["text"]) for r in rows]


def _index_state() -> dict[str, int]:
    """session_id -> the opencode updated timestamp already embedded."""
    with rag._connect() as conn:
        rows = conn.execute(
            """
            SELECT session_id,
                   MAX(json_extract(metadata, '$.updated')) AS updated
            FROM memory_chunks
            WHERE source = ? AND session_id IS NOT NULL
            GROUP BY session_id
            """,
            (SOURCE,),
        ).fetchall()
    state: dict[str, int] = {}
    for row in rows:
        try:
            state[row["session_id"]] = int(row["updated"])
        except (TypeError, ValueError):
            continue
    return state


def purge_private(limit: int = 1000) -> dict[str, int]:
    """Remove RAG data for any chat now marked private.

    Marking a chat private has to be retroactive: if it was embedded before the
    flag was set, the embeddings must go, otherwise the flag is decorative.
    """
    private = private_sessions()
    if not private:
        return {"scanned": 0, "purged_sessions": 0, "chunks": 0}

    with rag._connect() as conn:
        indexed = [
            row["session_id"]
            for row in conn.execute(
                "SELECT DISTINCT session_id FROM memory_chunks "
                "WHERE source = ? AND session_id IS NOT NULL",
                (SOURCE,),
            ).fetchall()
        ]

    purged, chunks = 0, 0
    for session_id in indexed[:limit]:
        if session_id in private:
            chunks += rag.delete_session(session_id)
            purged += 1
    return {"scanned": len(indexed), "purged_sessions": purged, "chunks": chunks}


def ingest_opencode(limit: int = 100, force: bool = False) -> dict[str, Any]:
    """Index recent opencode chats. Returns a summary of what changed."""
    private = private_sessions()

    purge = purge_private()
    state = _index_state()

    conn = _connect_readonly()
    if conn is None:
        return {
            "ok": False,
            "error": f"opencode db not found at {OPENCODE_DB}",
            "indexed_sessions": 0,
            "chunks": 0,
        }

    try:
        table = _sessions_table(conn)
        sessions = conn.execute(
            f"""
            SELECT id, title, time_updated
            FROM {table}
            WHERE time_archived IS NULL
            ORDER BY time_updated DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        indexed, chunks, skipped_private, unchanged = 0, 0, 0, 0

        for row in sessions:
            session_id = row["id"]
            title = row["title"] or "(untitled)"
            updated = int(row["time_updated"] or 0)

            if session_id in private:
                skipped_private += 1
                continue
            if not force and state.get(session_id) == updated:
                unchanged += 1
                continue

            turns = _turns_for_session(conn, session_id)
            if not turns:
                continue

            # Replace rather than append, so re-indexing an edited chat does
            # not leave the previous wording embedded alongside the new one.
            rag.delete_session(session_id)

            added = 0
            for role, text in turns:
                for piece in _split(text):
                    content = f"# {title}\n\n{piece}" if role == "user" else piece
                    rag.index_chunk(
                        source=SOURCE,
                        source_id=session_id,
                        session_id=session_id,
                        role=role,
                        content=content,
                        metadata={"title": title, "updated": updated},
                    )
                    added += 1

            indexed += 1
            chunks += added
    finally:
        conn.close()

    return {
        "ok": True,
        "candidates": len(sessions),
        "indexed_sessions": indexed,
        "chunks": chunks,
        "skipped_private": skipped_private,
        "unchanged": unchanged,
        "private_purged": purge["purged_sessions"],
        "private_purged_chunks": purge["chunks"],
    }