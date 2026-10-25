#!/usr/bin/env python3
"""MCP server that bridges opencode to local-brain RAG memory."""

import asyncio
import json
import urllib.parse
import urllib.request

from mcp.server.fastmcp import FastMCP

LOCAL_BRAIN_URL = "http://127.0.0.1:9876"

mcp = FastMCP("local-brain-rag")


def _http_json(method: str, path: str, payload: dict | None = None) -> dict:
    """Make an HTTP request to local-brain and return JSON."""
    url = f"{LOCAL_BRAIN_URL}{path}"
    if payload is not None:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
    else:
        req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def search_memory(query: str, k: int = 5) -> str:
    """Search the local-brain RAG memory for relevant information across all indexed sources (notes, documents, past opencode sessions, etc.).

    Args:
        query: The search query to find relevant memories.
        k: Number of results to return (default: 5).
    """
    data = _http_json("GET", f"/api/rag/search?q={urllib.parse.quote(query)}&k={k}")
    results = data.get("results", [])
    if not results:
        return "No relevant memories found."

    lines = []
    for r in results:
        source = r.get("source", "unknown")
        title = r.get("metadata", {}).get("title", "untitled")
        score = r.get("score", 0)
        content = r.get("content", "")
        # Truncate content to a reasonable preview
        preview = content[:800] + "..." if len(content) > 800 else content
        lines.append(f"[Source: {source} | Title: {title} | Score: {score:.3f}]\n{preview}")
    return "\n\n---\n\n".join(lines)


@mcp.tool()
def recent_context(session_id: str, n: int = 10) -> str:
    """Get recent indexed turns from a specific session.

    Args:
        session_id: The session ID to get context for.
        n: Number of recent turns to retrieve (default: 10).
    """
    data = _http_json("GET", f"/api/rag/recent?session_id={urllib.parse.quote(session_id)}&n={n}")
    results = data.get("results", [])
    if not results:
        return f"No recent context found for session {session_id}."

    lines = []
    for r in results:
        role = r.get("role", "unknown")
        content = r.get("content", "")
        preview = content[:600] + "..." if len(content) > 600 else content
        lines.append(f"[{role}]\n{preview}")
    return "\n\n---\n\n".join(lines)


@mcp.tool()
def index_memory(source: str, content: str, session_id: str | None = None, role: str | None = None) -> str:
    """Index a piece of content into local-brain RAG memory.

    Args:
        source: Source identifier (e.g., 'opencode', 'notes', 'research').
        content: Content to index.
        session_id: Optional session ID to associate with.
        role: Optional role (user, assistant, system).
    """
    payload = {
        "source": source,
        "source_id": session_id or "",
        "content": content,
        "session_id": session_id,
        "role": role,
        "metadata": {},
    }
    data = _http_json("POST", "/api/rag/index", payload)
    if "error" in data:
        return f"Error indexing: {data['error']}"
    return f"Indexed successfully. Created {data.get('chunks', 0)} chunk(s)."


async def main():
    await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
