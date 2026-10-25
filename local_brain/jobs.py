"""Read-only view of the job-radar database for the Jobs tab.

job-radar stays the only writer of its SQLite file; this module only issues
SELECTs. The connection is opened read-only where SQLite allows it, so a stray
write from the web process cannot corrupt a database that a scheduled ingest may
be holding open.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from local_brain import config

# Sort keys the UI may request, mapped to safe SQL fragments. Never interpolate
# a caller-supplied string into ORDER BY.
SORTS: Dict[str, str] = {
    "recent": "j.last_seen DESC",
    "oldest": "j.first_seen ASC",
    "title": "j.title COLLATE NOCASE ASC",
    "company": "j.company COLLATE NOCASE ASC",
    "salary": "j.salary_eur_yr DESC NULLS LAST",
    "score": "j.score DESC NULLS LAST",
    }

LIST_COLUMNS = """
    j.job_id, j.title, j.company, j.location, j.url, j.source,
    j.employment_type, j.seniority, j.categories,
    j.salary_min, j.salary_max, j.salary_currency, j.salary_period,
    j.salary_eur_yr, j.salary_is_known,
    j.remote, j.remote_type, j.location_restrictions, j.timezone_offsets,
    j.published_at, j.first_seen, j.last_seen, j.seen_count, j.status, j.score
"""


class JobRadarUnavailable(RuntimeError):
    """The job-radar database is missing or unreadable."""


def database_path() -> Path:
    return Path(config.get("jobs.database", str(Path.home() / "job-radar/data/jogradar.db")))


def _connect() -> sqlite3.Connection:
    path = database_path()
    if not path.exists():
        raise JobRadarUnavailable(f"job-radar database not found at {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise JobRadarUnavailable(f"cannot open {path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _fts_query(text: str) -> Optional[str]:
    """Turn free text into a safe FTS5 MATCH expression.

    Every token is wrapped in double quotes so operators like AND, OR, NEAR or
    a stray asterisk from the user cannot be parsed as FTS syntax -- an
    unescaped token makes MATCH raise, which would surface as a 500.
    """
    tokens = [t for t in re.split(r"[^\w]+", text or "", flags=re.UNICODE) if t]
    if not tokens:
        return None
    # A prefix match on the last token makes search feel responsive as you type.
    quoted = [f'"{t}"' for t in tokens[:-1]]
    quoted.append(f'"{tokens[-1]}"*')
    # AND, not OR: "python engineer" should mean both words, otherwise any job
    # containing "engineer" swamps the results and the search feels broken.
    return " AND ".join(quoted)


def _row_to_job(row: sqlite3.Row, *, with_description: bool = False) -> Dict[str, Any]:
    job = {
        "job_id": row["job_id"],
        "title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "url": row["url"],
        "source": row["source"],
        "employment_type": row["employment_type"],
        "seniority": row["seniority"],
        "categories": row["categories"],
        "salary_min": row["salary_min"],
        "salary_max": row["salary_max"],
        "salary_currency": row["salary_currency"],
        "salary_period": row["salary_period"],
        "salary_eur_yr": row["salary_eur_yr"],
        "salary_is_known": bool(row["salary_is_known"]),
        "remote": bool(row["remote"]),
        "remote_type": row["remote_type"],
        "location_restrictions": row["location_restrictions"],
        "timezone_offsets": row["timezone_offsets"],
        "published_at": row["published_at"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "seen_count": row["seen_count"],
        "status": row["status"],
        "score": row["score"],
    }
    if with_description:
        job["description_text"] = row["description_text"] or ""
    return job


def list_jobs(
    *,
    query: Optional[str] = None,
    remote: Optional[bool] = None,
    source: Optional[str] = None,
    salary_known: Optional[bool] = None,
    sort: str = "recent",
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """Page through jobs, newest first by default."""
    conn = _connect()
    try:
        if not _table_exists(conn, "jobs"):
            raise JobRadarUnavailable("job-radar database has no 'jobs' table")

        # Clamp before querying so the echoed page bounds match what was fetched.
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))

        where: List[str] = []
        params: List[Any] = []

        match = _fts_query(query) if query else None
        if match:
            if not _table_exists(conn, "jobs_fts"):
                raise JobRadarUnavailable("job-radar database has no FTS index")
            where.append(
                "j.job_id IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)"
            )
            params.append(match)

        # A text search is only useful if relevance drives the order. When a
        # query is active the fetch uses the bm25-ordered SQL built below and
        # ignores `sort`, so any requested sort is meaningless here.
        order_by = SORTS.get(sort, SORTS["recent"])

        if remote is not None:
            where.append("j.remote = ?")
            params.append(1 if remote else 0)

        if source:
            where.append("j.source = ?")
            params.append(source)

        if salary_known is not None:
            where.append("j.salary_is_known = ?")
            params.append(1 if salary_known else 0)

        clause = f"WHERE {' AND '.join(where)}" if where else ""

        # The row-fetching query joins the FTS table directly so `bm25()` in the
        # ORDER BY can resolve. The join must stay unaliased: SQLite's bm25()
        # auxiliary rejects a table alias ("no such column: f"). The count query
        # does not need the join because it only filters, so it reuses the same
        # WHERE and parameter list.
        if match:
            fetch_sql = (
                f"SELECT {LIST_COLUMNS} FROM jobs j "
                f"JOIN jobs_fts ON jobs_fts.rowid = j.job_id {clause} "
                f"ORDER BY bm25(jobs_fts), j.last_seen DESC LIMIT ? OFFSET ?"
            )
        else:
            fetch_sql = (
                f"SELECT {LIST_COLUMNS} FROM jobs j {clause} "
                f"ORDER BY {order_by} LIMIT ? OFFSET ?"
            )

        total = conn.execute(
            f"SELECT COUNT(*) FROM jobs j {clause}", params
        ).fetchone()[0]

        rows = conn.execute(fetch_sql, [*params, limit, offset]).fetchall()

        sources = [
            r["source"]
            for r in conn.execute(
                "SELECT DISTINCT source FROM jobs WHERE source IS NOT NULL ORDER BY source"
            ).fetchall()
        ]

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "query": query or None,
            "sort": sort if sort in SORTS else "recent",
            "sources": sources,
            "jobs": [_row_to_job(r) for r in rows],
        }
    finally:
        conn.close()


def get_job(job_id: int) -> Optional[Dict[str, Any]]:
    """Full record for one job, including its description."""
    conn = _connect()
    try:
        row = conn.execute(
            f"SELECT {LIST_COLUMNS}, j.description_text, j.dedup_key "
            f"FROM jobs j WHERE j.job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None

        job = _row_to_job(row, with_description=True)
        job["dedup_key"] = row["dedup_key"]

        # Other boards this same role was seen on; useful context when deciding
        # whether a listing is worth applying to.
        job["also_seen_on"] = [
            r["source"]
            for r in conn.execute(
                "SELECT DISTINCT source FROM jobs WHERE dedup_key = ? AND job_id != ? "
                "ORDER BY source",
                (row["dedup_key"], job_id),
            ).fetchall()
        ]

        # Any LLM score job-radar has recorded, if scoring has ever run.
        if _table_exists(conn, "scores"):
            score_row = conn.execute(
                "SELECT model, score, verdict, payload, created_at FROM scores "
                "WHERE job_id = ? ORDER BY created_at DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if score_row is not None:
                job["llm_score"] = {
                    "model": score_row["model"],
                    "score": score_row["score"],
                    "verdict": score_row["verdict"],
                    "payload": score_row["payload"],
                    "created_at": score_row["created_at"],
                }
        return job
    finally:
        conn.close()


def stats() -> Dict[str, Any]:
    """Headline numbers for the Jobs tab header."""
    conn = _connect()
    try:
        if not _table_exists(conn, "jobs"):
            raise JobRadarUnavailable("job-radar database has no 'jobs' table")

        totals = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(remote) AS remote,
                   SUM(salary_is_known) AS with_salary,
                   MAX(last_seen) AS newest,
                   MAX(first_seen) AS first_job
            FROM jobs
            """
        ).fetchone()

        by_source = {
            r["source"]: r["n"]
            for r in conn.execute(
                "SELECT source, COUNT(*) AS n FROM jobs GROUP BY source ORDER BY n DESC"
            ).fetchall()
        }

        scored = 0
        if _table_exists(conn, "scores"):
            scored = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]

        return {
            "total": totals["total"] or 0,
            "remote": totals["remote"] or 0,
            "with_salary": totals["with_salary"] or 0,
            "newest_seen": totals["newest"],
            "earliest_seen": totals["first_job"],
            "by_source": by_source,
            "scored": scored,
            "database": str(database_path()),
            "available": True,
        }
    finally:
        conn.close()


def health() -> Dict[str, Any]:
    """Never raises, so the tab can render a useful message when it is down."""
    try:
        return stats()
    except JobRadarUnavailable as exc:
        return {"available": False, "error": str(exc), "database": str(database_path())}


def page_size() -> int:
    return int(config.get("jobs.page_size", 50))