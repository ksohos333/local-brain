"""Read-only view of the campaign databases for the Leads tab.

`~/campaign` stays the only writer of leads.db and outreach.db; this module
only issues SELECTs, opened read-only where SQLite allows it. Tracking contact
state is the point of the tab, so writes are offered deliberately rather than
implied: `update_status` is the single exception and is only reachable through
the explicit PATCH route.

The two databases are separate files with no foreign key between them, so the
join is done in Python after fetching, which keeps a missing table from turning
into a SQL error.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from local_brain import config

# Sort keys the UI may request, mapped to safe SQL fragments. Never interpolate
# a caller-supplied string into ORDER BY.
SORTS: Dict[str, str] = {
    "recent": "l.created_at DESC, l.id DESC",
    "name": "l.name COLLATE NOCASE ASC",
    "city": "l.addr_city COLLATE NOCASE ASC",
    "status": "l.status COLLATE NOCASE ASC, l.name COLLATE NOCASE ASC",
    "contacted": "l.contacted_at DESC NULLS LAST",
}

# Statuses the campaign uses. `new` is the ingest default; the rest are what
# leadops writes via LeadDB.update_status / mark_contacted.
STATUSES = ("new", "queued", "contacted", "no_answer", "interested", "not_now", "rejected")

LIST_COLUMNS = """
    l.id, l.name, l.name_en, l.addr_city, l.area_name, l.lat, l.lon,
    l.website, l.phone, l.email_tag, l.brand, l.tags_json, l.hook,
    l.status, l.contacted_at, l.owner_name, l.salutation_en, l.salutation_gr,
    l.site_status, l.name_source, l.name_confidence, l.source, l.source_id,
    l.osm_type, l.osm_id, l.created_at, l.updated_at
"""


class LeadsUnavailable(RuntimeError):
    """A campaign database is missing or unreadable."""


def _campaign_root() -> Path:
    return Path(config.get("campaign.root", str(Path.home() / "campaign")))


def leads_path() -> Path:
    return _campaign_root() / "data" / "leads.db"


def outreach_path() -> Path:
    return _campaign_root() / "data" / "outreach.db"


def _connect_ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise LeadsUnavailable(f"database not found at {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise LeadsUnavailable(f"cannot open {path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    return conn


def _connect_rw(path: Path) -> sqlite3.Connection:
    """Only for the explicit status write; still uses a busy timeout."""
    if not path.exists():
        raise LeadsUnavailable(f"database not found at {path}")
    try:
        conn = sqlite3.connect(path, timeout=5.0)
    except sqlite3.Error as exc:
        raise LeadsUnavailable(f"cannot open {path}: {exc}") from exc
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


def _row_to_lead(row: sqlite3.Row) -> Dict[str, Any]:
    lead = dict(row)
    # tags_json is whatever the importer wrote; tolerate a non-list rather than
    # failing the whole request over one bad row.
    raw = lead.get("tags_json")
    if isinstance(raw, str):
        import json

        try:
            lead["tags"] = json.loads(raw)
        except (ValueError, TypeError):
            lead["tags"] = []
    else:
        lead["tags"] = []
    lead.pop("tags_json", None)
    return lead


def _reply_counts(lead_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """Count replies per lead, tolerating a missing/empty replies table."""
    if not lead_ids:
        return {}
    out: Dict[int, Dict[str, Any]] = {}
    try:
        conn = _connect_ro(outreach_path())
    except LeadsUnavailable:
        return out
    try:
        if not _table_exists(conn, "replies"):
            return out
        marks = ",".join("?" for _ in lead_ids)
        rows = conn.execute(
            f"""SELECT lead_id,
                       COUNT(*) AS total,
                       SUM(CASE WHEN reviewed_at IS NOT NULL THEN 1 ELSE 0 END) AS reviewed,
                       MAX(received_at) AS last_reply_at
                FROM replies WHERE lead_id IN ({marks}) GROUP BY lead_id""",
            lead_ids,
        ).fetchall()
        for r in rows:
            if r["lead_id"] is not None:
                out[int(r["lead_id"])] = {
                    "total": r["total"] or 0,
                    "reviewed": r["reviewed"] or 0,
                    "last_reply_at": r["last_reply_at"],
                }
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    return out


def list_leads(
    q: Optional[str] = None,
    status: Optional[str] = None,
    city: Optional[str] = None,
    area: Optional[str] = None,
    contacted: Optional[bool] = None,
    sort: str = "recent",
    limit: Optional[int] = None,
    offset: int = 0,
) -> Dict[str, Any]:
    conn = _connect_ro(leads_path())
    try:
        where: List[str] = []
        params: List[Any] = []
        if q:
            where.append(
                "(l.name LIKE ? OR l.name_en LIKE ? OR l.brand LIKE ?"
                " OR l.addr_city LIKE ? OR l.phone LIKE ? OR l.website LIKE ?"
                " OR l.hook LIKE ?)"
            )
            like = f"%{q}%"
            params.extend([like] * 7)
        if status:
            where.append("l.status = ?")
            params.append(status)
        if city:
            where.append("l.addr_city = ?")
            params.append(city)
        if area:
            where.append("l.area_name = ?")
            params.append(area)
        if contacted is True:
            where.append("l.contacted_at IS NOT NULL")
        elif contacted is False:
            where.append("l.contacted_at IS NULL")

        clause = ("WHERE " + " AND ".join(where)) if where else ""
        order = SORTS.get(sort, SORTS["recent"])

        total = conn.execute(
            f"SELECT COUNT(*) FROM leads l {clause}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"SELECT {LIST_COLUMNS} FROM leads l {clause} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()

        cities = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT addr_city FROM leads "
                "WHERE addr_city IS NOT NULL AND addr_city != '' "
                "ORDER BY addr_city COLLATE NOCASE"
            )
        ]
        areas = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT area_name FROM leads "
                "WHERE area_name IS NOT NULL AND area_name != '' "
                "ORDER BY area_name COLLATE NOCASE"
            )
        ]
        # Only offer statuses that actually occur, so the filter never returns
        # an empty tab for a value leadops has never used.
        statuses = [
            r[0] or "new"
            for r in conn.execute(
                "SELECT DISTINCT status FROM leads ORDER BY status COLLATE NOCASE"
            )
        ]
    except sqlite3.Error as exc:
        raise LeadsUnavailable(f"leads query failed: {exc}") from exc
    finally:
        conn.close()

    leads = [_row_to_lead(r) for r in rows]
    counts = _reply_counts([lead["id"] for lead in leads])
    for lead in leads:
        lead["replies"] = counts.get(lead["id"], {"total": 0, "reviewed": 0, "last_reply_at": None})

    return {
        "leads": leads,
        "total": total,
        "limit": limit,
        "offset": offset,
        "cities": cities,
        "areas": areas,
        "statuses": statuses,
    }


def get_lead(lead_id: int) -> Optional[Dict[str, Any]]:
    conn = _connect_ro(leads_path())
    try:
        row = conn.execute(
            f"SELECT {LIST_COLUMNS} FROM leads l WHERE l.id = ?", (lead_id,)
        ).fetchone()
    except sqlite3.Error as exc:
        raise LeadsUnavailable(f"leads query failed: {exc}") from exc
    finally:
        conn.close()
    if row is None:
        return None
    lead = _row_to_lead(row)
    lead["replies"] = _reply_counts([lead_id]).get(
        lead_id, {"total": 0, "reviewed": 0, "last_reply_at": None}
    )
    lead["reply_history"] = _reply_history(lead_id)
    return lead


def _reply_history(lead_id: int) -> List[Dict[str, Any]]:
    try:
        conn = _connect_ro(outreach_path())
    except LeadsUnavailable:
        return []
    try:
        if not _table_exists(conn, "replies"):
            return []
        rows = conn.execute(
            """SELECT from_addr, from_name, subject, category, outcome,
                      received_at, reviewed_at
               FROM replies WHERE lead_id = ? ORDER BY received_at DESC""",
            (lead_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def update_status(lead_id: int, status: str) -> Dict[str, Any]:
    """Set a lead's status, mirroring leadops.LeadDB.update_status.

    This is the one write local-brain performs. Touching `contacted_at` here
    too keeps the spreadsheet honest: if a lead moves to `contacted`, the
    timestamp records it, which is the column the campaign note says is
    currently empty despite real calls having happened.
    """
    if status not in STATUSES:
        raise LeadsUnavailable(
            f"status must be one of {', '.join(STATUSES)}, got {status!r}"
        )
    now = datetime.now().isoformat(timespec="seconds")
    conn = _connect_rw(leads_path())
    try:
        cur = conn.execute(
            "SELECT id FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        if cur is None:
            raise LeadsUnavailable(f"lead {lead_id} not found")
        if status == "contacted":
            conn.execute(
                "UPDATE leads SET status=?, contacted_at=?, updated_at=? WHERE id=?",
                (status, now, now, lead_id),
            )
        else:
            conn.execute(
                "UPDATE leads SET status=?, updated_at=? WHERE id=?",
                (status, now, lead_id),
            )
        conn.commit()
    except sqlite3.Error as exc:
        raise LeadsUnavailable(f"could not update lead {lead_id}: {exc}") from exc
    finally:
        conn.close()
    return {"ok": True, "id": lead_id, "status": status}


def stats() -> Dict[str, Any]:
    """Counts for the sidebar. Never raises: the tab shows outages inline."""
    health = {"available": False, "leads_db": str(leads_path()), "outreach_db": str(outreach_path())}
    try:
        conn = _connect_ro(leads_path())
    except LeadsUnavailable as exc:
        health["error"] = str(exc)
        return {**health, "total": 0, "by_status": {}, "by_area": {},
                "contacted": 0, "with_phone": 0, "with_website": 0, "replies": 0}

    try:
        by_status = {
            (r["status"] or "new"): r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM leads GROUP BY status ORDER BY n DESC"
            )
        }
        by_area = {
            (r["area_name"] or "unknown"): r["n"]
            for r in conn.execute(
                "SELECT area_name, COUNT(*) AS n FROM leads GROUP BY area_name "
                "ORDER BY n DESC LIMIT 12"
            )
        }
        total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        contacted = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE contacted_at IS NOT NULL"
        ).fetchone()[0]
        with_phone = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE phone IS NOT NULL AND phone != ''"
        ).fetchone()[0]
        with_website = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE website IS NOT NULL AND website != ''"
        ).fetchone()[0]
    except sqlite3.Error as exc:
        conn.close()
        return {**health, "error": str(exc), "total": 0, "by_status": {}, "by_area": {},
                "contacted": 0, "with_phone": 0, "with_website": 0, "replies": 0}
    conn.close()

    replies = 0
    try:
        oconn = _connect_ro(outreach_path())
        if _table_exists(oconn, "replies"):
            replies = oconn.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
        oconn.close()
    except LeadsUnavailable:
        replies = 0

    return {
        **health,
        "available": True,
        "total": total,
        "by_status": by_status,
        "by_area": by_area,
        "contacted": contacted,
        "with_phone": with_phone,
        "with_website": with_website,
        "replies": replies,
    }