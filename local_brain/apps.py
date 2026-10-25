"""Health and launch paths for the other local apps.

The brain is the hub these tools sit behind: it already talks to trueforge and
Ollama for chat, so it is the natural place to show whether the rest of the
local stack is actually up. Probes are deliberately cheap and parallel -- a
dashboard that blocks on five sequential timeouts is worse than no dashboard.

Two kinds of thing live here:

* **services** answer an HTTP request, so health is a real check.
* **apps** are GTK/desktop programs with no port to talk to. For those, health
  means "is a matching process running", and the launcher is a command.

Nothing here starts or stops anything on its own. Reporting is passive; the
launch entries are returned to the UI as commands the user has to invoke, so
local-brain cannot spawn apps behind the user's back.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional


# Each entry: what to call it, how to decide "up", and what runs it.
# `probe` is either a TCP host/port to connect to or a process name to look for.
APPS: List[Dict[str, Any]] = [
    {
        "id": "qwen",
        "name": "Local Qwen",
        "kind": "service",
        "detail": "llama-server (chat + embeddings)",
        "url": "http://127.0.0.1:8080/v1/models",
        "launch": None,
    },
    {
        "id": "diagnostic",
        "name": "Local Butler Diagnostic",
        "kind": "app",
        "detail": "system + AI diagnostics",
        "process": "local_butler.apps.diagnostic",
        "launch": "local-butler-diagnostic",
    },
    {
        "id": "calendar",
        "name": "Local Butler Calendar",
        "kind": "app",
        "detail": "CalDAV calendar (Radicale)",
        "process": "local_butler.apps.calendar",
        "launch": "local-butler-calendar",
        "port": 5232,
        "url": "http://127.0.0.1:5232/",
    },
    {
        "id": "bigus_dickus",
        "name": "bigus dickus",
        "kind": "service",
        "detail": "agent MCP + desktop server",
        "port": 8780,
        "url": "http://127.0.0.1:8780/",
        "process": "agent.mcp_server",
        # Its .desktop entry execs the script directly and nothing is installed
        # on PATH, so resolve it as an absolute path.
        "launch": "/home/bpoppa12/.local/bin/bigus_dickus",
    },
    {
        "id": "geonews",
        "name": "Geonews",
        "kind": "service",
        "detail": "news backend + GTK client",
        "port": 8788,
        "url": "http://127.0.0.1:8788/",
        "process": "local_butler.apps.geonews",
        "launch": "geonews",
    },
    # TEMPORARY: Odysseus row so it shows in the Local Apps dashboard.
    # Remove this dict once Odysseus gets a permanent home.
    {
        "id": "odysseus",
        "name": "Odysseus",
        "kind": "service",
        "detail": "AI workspace (chat, agents, research)",
        "port": 7000,
        "url": "http://127.0.0.1:7000/",
        "process": "apps/odysseus/venv/bin/python",
        "launch": "/home/bpoppa12/apps/odysseus/launch.sh",
    },
]


def _tcp_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """True if something accepts a TCP connection on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def _http_ok(url: str, timeout: float = 2.5) -> Optional[bool]:
    """GET url; True on any response, False on a connection failure.

    A non-200 still counts as up: these services often answer / with a 404 or
    a redirect, which means the process is alive and reachable. Only being
    unable to connect is treated as down.
    """
    try:
        import httpx
    except ImportError:
        return _tcp_open(*_hostport(url))

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            await client.get(url)
            return True
    except Exception:
        return False


def _hostport(url: str) -> tuple:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.hostname or "127.0.0.1", parsed.port or 80


def _process_running(needle: str) -> bool:
    """Look for a running process whose cmdline contains needle."""
    try:
        from pathlib import Path

        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                    "utf-8", "replace"
                )
            except (OSError, PermissionError):
                continue
            if needle in cmdline and "local_brain" not in cmdline:
                return True
    except OSError:
        pass
    return False


def _launchable(command: Optional[str]) -> bool:
    """True if the launch target exists and is executable.

    Absolute paths are checked directly; bare names go through PATH, since the
    .desktop entries use both forms.
    """
    if not command:
        return False
    if "/" in command:
        path = Path(command).expanduser()
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(command) is not None


async def _probe(app: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve one app's status dict without ever raising."""
    result = {
        "id": app["id"],
        "name": app["name"],
        "kind": app["kind"],
        "detail": app["detail"],
        "up": False,
        "launch": app.get("launch"),
        "launchable": _launchable(app.get("launch")),
        "note": None,
    }

    try:
        url = app.get("url")
        port = app.get("port")

        if url:
            http_ok = await _http_ok(url)
        elif port:
            http_ok = _tcp_open("127.0.0.1", port)
        else:
            http_ok = None

        if http_ok is not None:
            result["up"] = http_ok
        else:
            # No port to check, so fall back to the process table.
            needle = app.get("process")
            if not needle:
                result["note"] = "no probe configured"
                return result
            result["up"] = await asyncio.to_thread(_process_running, needle)

        # A service can be listening but its process name may still be worth
        # reporting when the HTTP probe disagreed, so cross-check cheaply.
        if not result["up"] and app.get("process"):
            if await asyncio.to_thread(_process_running, app["process"]):
                result["note"] = "process alive but not answering on its port"
    except Exception as exc:  # noqa: BLE001 - a probe must never break the page
        result["note"] = f"probe failed: {type(exc).__name__}"

    return result


async def status() -> Dict[str, Any]:
    """Report every app in parallel and return a summary payload."""
    apps = await asyncio.gather(*(_probe(app) for app in APPS))
    return {
        "apps": list(apps),
        "up": sum(1 for a in apps if a["up"]),
        "total": len(apps),
    }