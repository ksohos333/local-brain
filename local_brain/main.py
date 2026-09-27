"""FastAPI backend for local-brain."""

import sys
import threading
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from local_brain import chat, rag, vault

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


@app.get("/api/search")
def search(q: str):
    return {"results": vault.search_notes(q)}


@app.get("/api/graph")
def graph():
    return vault.graph_data()


@app.get("/api/backlinks/{note_path:path}")
def note_backlinks(note_path: str):
    return {"backlinks": vault.backlinks(note_path)}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
