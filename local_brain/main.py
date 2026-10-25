"""FastAPI backend for local-brain."""

import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from local_brain import apps, caldav, chat, jobs, leads, logseq, rag, vault

app = FastAPI(title="local-brain", version="0.1.0")

STATIC_DIR = Path(__file__).parent / "static"


class NoteContent(BaseModel):
    content: str


class NotePath(BaseModel):
    path: str


class IndexRequest(BaseModel):
    source: str
    source_id: Optional[str] = None
    session_id: Optional[str] = None
    role: Optional[str] = None
    content: str
    metadata: Optional[dict] = None


class SearchRequest(BaseModel):
    query: str
    k: int = 5


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    model: Optional[str] = None


class TodoCreate(BaseModel):
    summary: str
    due: Optional[str] = None
    description: str = ""
    priority: Optional[int] = None


class TodoUpdate(BaseModel):
    summary: Optional[str] = None
    due: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[int] = None
    done: Optional[bool] = None


class LogseqNoteCreate(BaseModel):
    title: str
    kind: str = "page"  # page | journal
    tags: str = ""
    content: str = ""


@app.on_event("startup")
def startup():
    rag.init_schema()


@app.get("/api/rag/stats")
def rag_stats():
    return rag.stats()


@app.post("/api/rag/index")
def rag_index(req: IndexRequest):
    try:
        chunk_id = rag.index_chunk(
            source=req.source,
            source_id=req.source_id,
            session_id=req.session_id,
            role=req.role,
            content=req.content,
            metadata=req.metadata,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "id": chunk_id}


@app.get("/api/rag/search")
def rag_search(q: str, k: int = 5):
    try:
        return {"results": rag.search(q, k=k)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/rag/recent")
def rag_recent(session_id: str, n: int = 10):
    try:
        return {"results": rag.recent_context(session_id, n=n)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/rag/session/{session_id}")
def rag_delete_session(session_id: str):
    """Permanently drop every RAG chunk belonging to a chat session."""
    try:
        removed = rag.delete_session(session_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "session_id": session_id, "removed": removed}


@app.post("/api/rag/ingest-opencode")
def rag_ingest_opencode(background_tasks: BackgroundTasks):
    """Index recent opencode chats into memory, skipping private ones."""
    background_tasks.add_task(_ingest_opencode_task)
    return {"ok": True, "message": "Ingesting opencode chats in background."}


@app.get("/api/rag/ingest-opencode/status")
def rag_ingest_status():
    """Report how much of opencode's history is currently embedded."""
    try:
        from local_brain import ingest

        with ingest.rag._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM memory_chunks WHERE source = ?", (ingest.SOURCE,)
            ).fetchone()[0]
            sessions = conn.execute(
                "SELECT COUNT(DISTINCT session_id) FROM memory_chunks "
                "WHERE source = ? AND session_id IS NOT NULL",
                (ingest.SOURCE,),
            ).fetchone()[0]
        return {
            "source": ingest.SOURCE,
            "chunks": total,
            "sessions": sessions,
            "private": sorted(ingest.private_sessions()),
            "private_count": len(ingest.private_sessions()),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Single-flight guard: embedding is expensive and a second overlapping run
# would re-embed the same chats.
_INGEST_LOCK = threading.Lock()


def _ingest_opencode_task():
    from local_brain import ingest

    if not _INGEST_LOCK.acquire(blocking=False):
        print("[ingest] already in progress; skipping duplicate request", file=sys.stderr)
        return
    try:
        summary = ingest.ingest_opencode()
        print(f"[ingest] {summary}", file=sys.stderr)
    except Exception as exc:
        print(f"[ingest] failed: {exc}", file=sys.stderr)
    finally:
        _INGEST_LOCK.release()


@app.post("/api/rag/reindex-vault")
def rag_reindex_vault(background_tasks: BackgroundTasks):
    background_tasks.add_task(_reindex_vault_task)
    return {"ok": True, "message": "Reindexing vault in background."}


# Single-flight guard. Background tasks run in a threadpool, so overlapping
# reindex requests would otherwise each delete and re-embed the whole vault.
_REINDEX_LOCK = threading.Lock()


def _reindex_vault_task():
    from pathlib import Path

    if not _REINDEX_LOCK.acquire(blocking=False):
        print("[reindex] already in progress; skipping duplicate request", file=sys.stderr)
        return
    try:
        brain_root = Path.home() / "brain"
        sources = {
            "local-brain": brain_root / "local-brain",
            "dendron": brain_root / "dendron" / "vault",
            "logseq": brain_root / "logseq",
        }
        for source, root in sources.items():
            if not root.exists():
                continue
            try:
                rag.delete_source(source)
                rag.index_vault_directory(root, source=source)
            except Exception as exc:
                print(f"[reindex] failed for {source}: {exc}", file=sys.stderr)
        # Compact the FTS5 index afterwards, otherwise the segment bloat from
        # this delete+insert cycle accumulates on every run.
        try:
            rag.optimize_index()
        except Exception as exc:
            print(f"[reindex] optimize failed: {exc}", file=sys.stderr)
    finally:
        _REINDEX_LOCK.release()


@app.get("/api/models")
async def list_models():
    try:
        models = await chat.get_models()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"models": models}


@app.get("/api/chat/health")
async def chat_health():
    try:
        tf_ok, tf_models = await chat.trueforge_health()
        ollama_ok, ollama_models = await chat.ollama_health()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "backends": {
            "trueforge": {"ok": tf_ok, "models": tf_models},
            "ollama": {"ok": ollama_ok, "models": ollama_models},
        }
    }


@app.get("/api/context")
def get_context(message: str, k: int = 4):
    try:
        chunks = rag.search(message, k=k)
        results = []
        for c in chunks:
            meta = c.get("metadata", {}) or {}
            results.append({
                "source": c.get("source"),
                "title": meta.get("title", c.get("source_id", "")),
                "path": meta.get("path"),
                "content": c.get("content", "")[:500],
                "score": c.get("score", 0),
            })
        return {"chunks": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    async def event_generator():
        async for event in chat.chat_stream(
            message=req.message,
            local_session_id=req.session_id,
            model=req.model,
        ):
            yield event

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
    )


@app.get("/api/tree")
def get_tree():
    return {"tree": vault.tree()}


@app.get("/api/notes")
def list_notes():
    notes = vault.list_notes()
    return {"notes": [{"path": n.path, "title": n.title} for n in notes]}


@app.get("/api/notes/{note_path:path}")
def read_note(note_path: str):
    try:
        content = vault.read_note(note_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"path": note_path, "content": content}


@app.put("/api/notes/{note_path:path}")
def save_note(note_path: str, body: NoteContent):
    try:
        vault.save_note(note_path, body.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "path": note_path}


@app.post("/api/notes")
def create_note(body: NotePath):
    try:
        vault.create_note(body.path)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="Note already exists")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "path": body.path}


@app.delete("/api/notes/{note_path:path}")
def delete_note(note_path: str):
    try:
        vault.delete_note(note_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"ok": True}


@app.get("/api/logseq/status")
def logseq_status():
    return logseq.status()


@app.post("/api/logseq/notes")
def create_logseq_note(body: LogseqNoteCreate):
    if not logseq.status()["reachable"]:
        raise HTTPException(status_code=503, detail="Logseq vault is not available")
    try:
        return logseq.create_note(
            title=body.title,
            kind=body.kind,
            tags=body.tags,
            content=body.content,
        )
    except logseq.LogseqError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"could not write note: {e}")


@app.get("/api/search")
def search(q: str):
    return {"results": vault.search_notes(q)}


@app.get("/api/graph")
def graph():
    return vault.graph_data()


@app.get("/api/backlinks/{note_path:path}")
def note_backlinks(note_path: str):
    return {"backlinks": vault.backlinks(note_path)}


@app.get("/api/apps")
async def apps_status():
    """Health of the sibling local apps and the local model server."""
    return await apps.status()


# --- Jobs (read-only view of job-radar's database) ---


@app.get("/api/jobs")
def jobs_list(
    q: Optional[str] = None,
    remote: Optional[bool] = None,
    source: Optional[str] = None,
    salary_known: Optional[bool] = None,
    sort: str = "recent",
    limit: int = 50,
    offset: int = 0,
):
    try:
        return jobs.list_jobs(
            query=q,
            remote=remote,
            source=source,
            salary_known=salary_known,
            sort=sort,
            limit=limit,
            offset=offset,
        )
    except jobs.JobRadarUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/jobs/stats")
def jobs_stats():
    # health() never raises, so the tab can show the outage inline.
    return jobs.health()


@app.get("/api/jobs/{job_id}")
def jobs_detail(job_id: int):
    try:
        job = jobs.get_job(job_id)
    except jobs.JobRadarUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job}


# --- Leads (Kalo Concierge research pipeline) ---


@app.get("/api/leads")
def leads_list(
    q: Optional[str] = None,
    status: Optional[str] = None,
    city: Optional[str] = None,
    area: Optional[str] = None,
    contacted: Optional[bool] = None,
    sort: str = "recent",
    limit: int = 50,
    offset: int = 0,
):
    try:
        return leads.list_leads(
            q=q, status=status, city=city, area=area,
            contacted=contacted, sort=sort, limit=limit, offset=offset,
        )
    except leads.LeadsUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/leads/stats")
def leads_stats():
    # stats() never raises, so the tab can show the outage inline.
    return leads.stats()


@app.get("/api/leads/{lead_id}")
def leads_detail(lead_id: int):
    try:
        lead = leads.get_lead(lead_id)
    except leads.LeadsUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"lead": lead}


class LeadStatusUpdate(BaseModel):
    status: str


@app.patch("/api/leads/{lead_id}")
def leads_update(lead_id: int, body: LeadStatusUpdate):
    # The one write local-brain makes to ~/campaign, and only because tracking
    # contact state is the whole point of the tab.
    try:
        return leads.update_status(lead_id, body.status)
    except leads.LeadsUnavailable as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Calendar (read-only, from Radicale) ---


@app.get("/api/calendar/status")
def calendar_status():
    return caldav.health()


@app.get("/api/calendar/events")
def calendar_events(start: Optional[str] = None, end: Optional[str] = None):
    """Events overlapping [start, end).

    Both bounds are inclusive calendar dates from the month grid, so the window
    is floored to the start of `start` and widened to cover all of `end`.
    """
    try:
        if start:
            window_start = datetime.fromisoformat(start)
        else:
            window_start = datetime.today()
        window_start = window_start.replace(hour=0, minute=0, second=0, microsecond=0)

        if not start:
            window_start = window_start.replace(day=1)

        if end:
            window_end = datetime.fromisoformat(end) + timedelta(days=1)
        else:
            nxt = (window_start.replace(day=28) + timedelta(days=4)).replace(day=1)
            window_end = nxt
        window_end = window_end.replace(hour=0, minute=0, second=0, microsecond=0)
    except ValueError:
        raise HTTPException(status_code=400, detail="start/end must be YYYY-MM-DD")

    try:
        events = caldav.fetch_events(window_start, window_end)
    except caldav.CalDAVError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {
        "start": window_start.date().isoformat(),
        "end": (window_end - timedelta(days=1)).date().isoformat(),
        "events": events,
    }


# --- To-dos (VTODO, synced through Radicale) ---


@app.get("/api/todos")
def todos_list():
    try:
        return {"todos": caldav.list_todos()}
    except caldav.CalDAVError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/todos")
def todos_create(body: TodoCreate):
    try:
        todo = caldav.create_todo(
            summary=body.summary,
            due=body.due,
            description=body.description,
            priority=body.priority,
        )
    except caldav.CalDAVError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"todo": todo}


@app.patch("/api/todos/{resource:path}")
def todos_update(resource: str, body: TodoUpdate):
    try:
        todo = caldav.update_todo(
            resource,
            summary=body.summary,
            due=body.due,
            description=body.description,
            priority=body.priority,
            done=body.done,
        )
    except caldav.CalDAVError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"todo": todo}


@app.delete("/api/todos/{resource:path}")
def todos_delete(resource: str):
    try:
        caldav.delete_todo(resource)
    except caldav.CalDAVError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
