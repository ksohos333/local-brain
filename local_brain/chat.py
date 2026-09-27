"""Chat orchestration for local-brain: RAG + TrueForge proxy."""

import json
import os
import uuid
from typing import AsyncGenerator, Dict, List, Optional, Tuple

import httpx

from local_brain import config, rag

TRUEFORGE_ENABLED = config.get("trueforge.enabled", True)
DEFAULT_CHAT_PROVIDER = config.get("chat.provider", "auto")  # auto | trueforge | ollama
DEFAULT_OLLAMA_MODEL = config.get("chat.ollama_model", "llama3.2:3b")


TRUEFORGE_URL = config.get("trueforge.url", "http://localhost:8790")
DEFAULT_MCP_SERVERS = ["bigus-dickus-local"]

# In-memory mapping from local session id -> TrueForge session id
_TRUEFORGE_SESSIONS: Dict[str, str] = {}


def _tf_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=TRUEFORGE_URL, timeout=httpx.Timeout(300.0))


async def trueforge_health() -> Tuple[bool, List[str]]:
    """Return (healthy, list_of_model_names)."""
    if not TRUEFORGE_ENABLED:
        return False, []
    try:
        async with _tf_client() as client:
            resp = await client.get("/api/v1/models", timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                models = [m.get("name") for m in data.get("data", []) if m.get("name")]
                return True, models
    except Exception:
        pass
    return False, []


async def ollama_health() -> Tuple[bool, List[str]]:
    """Return (healthy, list_of_model_names)."""
    ollama_url = config.get("ollama.url", "http://localhost:11434")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
            if resp.status_code == 200:
                models = [m.get("name", "") for m in resp.json().get("models", []) if m.get("name")]
                return True, models
    except Exception:
        pass
    return False, []


async def get_models() -> List[Dict[str, any]]:
    """Merge Ollama local models with configured models."""
    models = []
    seen = set()

    # Ollama models
    ollama_url = config.get("ollama.url", "http://localhost:11434")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                for m in data.get("models", []):
                    name = m.get("name", "")
                    if name and name not in seen:
                        seen.add(name)
                        models.append({"id": name, "name": name, "local": True, "provider": "ollama"})
    except Exception:
        pass

    # Config models
    for m in config.models_from_config():
        mid = m.get("name") or m.get("model")
        if mid and mid not in seen:
            seen.add(mid)
            models.append({
                "id": mid,
                "name": m.get("name", mid),
                "local": False,
                "provider": m.get("provider", "openai"),
                "model": m.get("model"),
                "base_url": m.get("base_url"),
            })

    # Fallback default TrueForge model so the dropdown is never empty
    if not models:
        default_model = config.get("trueforge.model", "local-llama/qwen3-8b-local")
        models.append({
            "id": default_model,
            "name": default_model,
            "local": True,
            "provider": "trueforge",
        })

    return models


async def _get_or_create_tf_session(
    local_session_id: str,
    model: Optional[str] = None,
) -> str:
    """Get or create a TrueForge session for the given local session id."""
    tf_id = _TRUEFORGE_SESSIONS.get(local_session_id)
    if tf_id:
        return tf_id

    instructions = (
        "You are a helpful local AI assistant with access to the user's digital brain. "
        "You may receive retrieved context from prior notes and conversations. "
        "Use it if relevant; otherwise answer normally. "
        "Be concise. Ask for clarification when needed. "
        "Request approval before destructive actions."
    )

    spec = {
        "model": {"name": model or config.get("trueforge.model", "local-llama/qwen3-8b-local")},
        "instructions": instructions,
        "mcp_servers": [
            {"name": name, "enable_tools": ["@all"], "require_approval_for_tools": ["@destructive"]}
            for name in config.get("trueforge.mcp_servers", DEFAULT_MCP_SERVERS)
        ],
        "config": {
            "iteration_limit": 25,
            "sandbox": {"enabled": True, "file_downloads": True},
            "dynamic_sub_agents": {"enabled": True},
            "context_management": {
                "compaction": {"enabled": True},
                "large_tool_response": {"enabled": True},
            },
            "generative_ui": {"enabled": False},
            "ask_user_questions": {"enabled": True},
            "web_search": {"enabled": False},
        },
    }

    async with _tf_client() as client:
        resp = await client.post(
            "/api/v1/sessions",
            json={
                "agent": {"spec": spec},
                "metadata": {"name": "local-brain-chat", "local_session_id": local_session_id},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        tf_id = data["data"]["id"]

    _TRUEFORGE_SESSIONS[local_session_id] = tf_id
    return tf_id


def _build_context_message(message: str, chunks: List[Dict]) -> str:
    """Build a context block to prepend to the user's message."""
    if not chunks:
        return message

    context_parts = ["Use the following retrieved context from the user's brain if relevant. Ignore it if not.\n"]
    for i, chunk in enumerate(chunks, 1):
        source = chunk.get("source", "unknown")
        title = chunk.get("metadata", {}).get("title", chunk.get("source_id", ""))
        content = chunk.get("content", "").strip()
        if not content:
            continue
        context_parts.append(f"[{i}] Source: {source} | {title}\n{content}\n")

    context = "\n".join(context_parts)
    return f"{context}\n---\nUser message:\n{message}"


def _pick_ollama_model(requested: Optional[str], available: List[str]) -> Optional[str]:
    """Pick an Ollama model, respecting a user request if valid."""
    if not available:
        return None
    if requested:
        model = requested.split("/", 1)[-1]
        if model in available:
            return model
    if DEFAULT_OLLAMA_MODEL in available:
        return DEFAULT_OLLAMA_MODEL
    return available[0]


async def _ollama_chat_stream(
    message: str,
    local_session_id: str,
    model: Optional[str],
) -> AsyncGenerator[str, None]:
    """Stream a chat response from Ollama with RAG context."""
    healthy, available = await ollama_health()
    if not healthy:
        yield _sse_event({"type": "error", "content": "Ollama is not reachable."})
        return
    chosen_model = _pick_ollama_model(model, available)
    if not chosen_model:
        yield _sse_event({"type": "error", "content": "No Ollama models available."})
        return
    ollama_url = config.get("ollama.url", "http://localhost:11434")
    assistant_text = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            async with client.stream(
                "POST",
                f"{ollama_url}/api/generate",
                json={
                    "model": chosen_model,
                    "prompt": message,
                    "stream": True,
                    "options": {"temperature": 0.7},
                },
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield _sse_event({"type": "error", "content": body.decode("utf-8", errors="ignore")})
                    return
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("done"):
                        break
                    token = event.get("response", "")
                    assistant_text += token
                    if token:
                        yield _sse_event({"type": "delta", "content": token})
    except Exception as exc:
        yield _sse_event({"type": "error", "content": f"Ollama error: {exc}"})
        return
    if assistant_text.strip():
        rag.index_turn(local_session_id, "assistant", assistant_text.strip())
    else:
        yield _sse_event({"type": "error", "content": "Ollama returned an empty response."})


async def _trueforge_chat_stream(
    message: str,
    local_session_id: str,
    model: Optional[str],
) -> AsyncGenerator[str, None]:
    """Stream a chat response from TrueForge with RAG context."""
    try:
        tf_session_id = await _get_or_create_tf_session(local_session_id, model=model)
    except Exception as exc:
        yield _sse_event({"type": "error", "content": f"Failed to create TrueForge session: {exc}"})
        return
    assistant_text = ""
    try:
        async with _tf_client() as client:
            async with client.stream(
                "POST",
                f"/api/v1/sessions/{tf_session_id}/turns",
                json={
                    "input": [{"type": "user.message", "content": message}],
                    "stream": True,
                },
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield _sse_event({"type": "error", "content": body.decode("utf-8", errors="ignore")})
                    return
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if not data:
                            continue
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        etype = event.get("type", "")
                        if etype == "model.message.delta":
                            content = event.get("content", "")
                            assistant_text += content
                            if content:
                                yield _sse_event({"type": "delta", "content": content})
                        elif etype == "model.message":
                            content = event.get("content", "")
                            assistant_text += content
                            if content:
                                yield _sse_event({"type": "delta", "content": content})
                        elif etype == "tool.approval_required":
                            yield _sse_event({"type": "tool_calls", "tool_calls": event.get("tool_calls", [])})
                        elif etype == "tool.response":
                            yield _sse_event({"type": "tool_response", "content": event.get("content", "")})
                        elif etype == "turn.done":
                            break
    except Exception as exc:
        yield _sse_event({"type": "error", "content": f"TrueForge error: {exc}"})
        return
    if assistant_text.strip():
        rag.index_turn(local_session_id, "assistant", assistant_text.strip())


async def chat_stream(
    message: str,
    local_session_id: Optional[str] = None,
    model: Optional[str] = None,
    k: int = 4,
) -> AsyncGenerator[str, None]:
    """Stream a chat response using TrueForge or Ollama, with RAG context."""
    if not local_session_id:
        local_session_id = uuid.uuid4().hex

    yield _sse_event({"type": "session", "session_id": local_session_id})

    # Retrieve context
    chunks = rag.search(message, k=k)

    # Build prompt with context
    augmented_message = _build_context_message(message, chunks)

    # Save user turn to memory
    rag.index_turn(local_session_id, "user", message)

    # Choose provider
    provider = DEFAULT_CHAT_PROVIDER
    requested_model = model
    # A client may send "ollama/model" or "trueforge/model" to force a backend.
    if requested_model and "/" in requested_model:
        maybe_provider, maybe_model = requested_model.split("/", 1)
        if maybe_provider in ("ollama", "trueforge"):
            provider = maybe_provider
            model = maybe_model

    if provider == "auto":
        tf_ok, _ = await trueforge_health()
        ollama_ok, _ = await ollama_health()
        if ollama_ok:
            provider = "ollama"
        elif tf_ok:
            provider = "trueforge"
        else:
            yield _sse_event({"type": "error", "content": "No AI backend available."})
            return
    elif provider == "trueforge":
        tf_ok, _ = await trueforge_health()
        if not tf_ok:
            yield _sse_event({"type": "error", "content": "TrueForge is configured but not reachable."})
            return
    elif provider == "ollama":
        ollama_ok, _ = await ollama_health()
        if not ollama_ok:
            yield _sse_event({"type": "error", "content": "Ollama is configured but not reachable."})
            return

    # Stream from selected backend
    if provider == "trueforge":
        async for event in _trueforge_chat_stream(augmented_message, local_session_id, model):
            yield event
    else:
        async for event in _ollama_chat_stream(augmented_message, local_session_id, model):
            yield event


def _sse_event(data: Dict) -> str:
    return f"data: {json.dumps(data)}\n\n"
