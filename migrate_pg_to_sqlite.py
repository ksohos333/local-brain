#!/usr/bin/env python3
"""Migrate RAG data from Postgres+pgvector to SQLite+sqlite-vec.

Usage:
    cd /home/bpoppa12/local-brain
    .venv/bin/python migrate_pg_to_sqlite.py
"""

import json
import struct
from pathlib import Path

import psycopg2

from local_brain import rag


PG_HOST = "127.0.0.1"
PG_PORT = 5432
PG_DB = "brainrag"
PG_USER = "raguser"
PG_PASS = "localrag"


def _parse_pg_vector(text: str) -> list[float]:
    """Parse Postgres vector text representation like '[0.1,0.2,...]'."""
    text = text.strip()
    if text == "None" or not text or text == "NULL":
        return []
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text:
        return []
    return [float(x) for x in text.split(",")]


def migrate():
    print("Connecting to Postgres...")
    pg = psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        dbname=PG_DB,
        user=PG_USER,
        password=PG_PASS,
    )
    cur = pg.cursor()
    cur.execute(
        """
        SELECT id, source, source_id, session_id, role, content, metadata,
               created_at, embedding
        FROM memory_chunks
        ORDER BY id
        """
    )
    rows = cur.fetchall()
    pg.close()
    print(f"Found {len(rows)} chunks in Postgres")

    # Ensure SQLite schema exists
    rag.init_schema()

    # Wipe any existing SQLite data to avoid duplicates
    stats_before = rag.stats()
    print("SQLite stats before migration:", stats_before)
    if stats_before["total"] > 0:
        print("Existing SQLite chunks found; wiping before migration...")
        with rag._connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM memory_chunks").fetchall()]
            for chunk_id in ids:
                conn.execute("DELETE FROM memory_chunks_vec WHERE rowid = ?", (chunk_id,))
                conn.execute(
                    "INSERT INTO memory_chunks_fts(memory_chunks_fts, rowid, content) VALUES ('delete', ?, NULL)",
                    (chunk_id,),
                )
            conn.execute("DELETE FROM memory_chunks")
            conn.commit()

    for i, row in enumerate(rows):
        chunk_id, source, source_id, session_id, role, content, metadata, created_at, embedding = row
        metadata_dict = metadata if isinstance(metadata, dict) else (json.loads(metadata) if metadata else {})
        created_iso = created_at.isoformat() if created_at else None
        vector = _parse_pg_vector(embedding) if embedding else None

        try:
            rag.import_chunk(
                chunk_id=chunk_id,
                source=source,
                source_id=source_id,
                session_id=session_id,
                role=role,
                content=content,
                metadata=metadata_dict,
                created_at=created_iso,
                vector=vector,
            )
        except Exception as exc:
            print(f"Failed to import chunk {chunk_id}: {exc}")
            continue

        if (i + 1) % 200 == 0:
            print(f"  migrated {i + 1}/{len(rows)} chunks")

    print(f"Migration complete: {len(rows)} chunks")
    print("SQLite stats after migration:", rag.stats())


if __name__ == "__main__":
    migrate()
