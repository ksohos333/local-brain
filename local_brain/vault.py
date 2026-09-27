"""Vault file operations for local-brain."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from local_brain import config


@dataclass
class Note:
    path: str
    title: str
    is_dir: bool = False


def _resolve_path(relative_path: str) -> Path:
    """Resolve a vault-relative path to a real filesystem path."""
    parts = relative_path.strip("/").split("/")
    if any(p in ("..", ".") or p.startswith(".") for p in parts):
        raise ValueError("Invalid path")
    return config.VAULT_ROOT.joinpath(*parts)


def list_notes() -> List[Note]:
    """Return flat list of all markdown notes in the vault."""
    notes = []
    for ext in ("*.md", "*.markdown"):
        for p in sorted(config.VAULT_ROOT.rglob(ext)):
            rel = p.relative_to(config.VAULT_ROOT).as_posix()
            notes.append(Note(path=rel, title=_extract_title(p)))
    return notes


def tree() -> List[dict]:
    """Return nested tree of files and directories."""
    root = {"name": "local-brain", "path": "", "is_dir": True, "children": []}
    for p in sorted(config.VAULT_ROOT.rglob("*")):
        rel = p.relative_to(config.VAULT_ROOT)
        parts = rel.parts
        node = root
        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            found = next((c for c in node["children"] if c["name"] == part), None)
            if found is None:
                child = {
                    "name": part,
                    "path": "/".join(parts[: i + 1]),
                    "is_dir": not is_last or p.is_dir(),
                    "children": [],
                }
                node["children"].append(child)
                node["children"].sort(key=lambda x: (not x["is_dir"], x["name"]))
                node = child
            else:
                node = found
    return [root]


def read_note(relative_path: str) -> str:
    path = _resolve_path(relative_path)
    if not path.exists():
        raise FileNotFoundError(relative_path)
    return path.read_text(encoding="utf-8")


def save_note(relative_path: str, content: str) -> None:
    path = _resolve_path(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def create_note(relative_path: str, content: str = "") -> None:
    path = _resolve_path(relative_path)
    if path.exists():
        raise FileExistsError(relative_path)
    if not content.strip():
        title = path.stem.replace("-", " ").replace("_", " ").title()
        content = f"# {title}\n\n"
    save_note(relative_path, content)


def delete_note(relative_path: str) -> None:
    path = _resolve_path(relative_path)
    path.unlink()


def search_notes(query: str) -> List[dict]:
    """Case-insensitive substring search across note contents."""
    query_lower = query.lower()
    results = []
    for p in config.VAULT_ROOT.rglob("*.md"):
        text = p.read_text(encoding="utf-8", errors="ignore")
        if query_lower in text.lower():
            rel = p.relative_to(config.VAULT_ROOT).as_posix()
            idx = text.lower().find(query_lower)
            preview = text[max(0, idx - 80) : idx + 200].replace("\n", " ")
            results.append(
                {
                    "path": rel,
                    "title": _extract_title(p),
                    "preview": preview,
                }
            )
    return results


def extract_wiki_links(content: str) -> List[str]:
    """Find [[note title]] links in content."""
    return re.findall(r"\[\[([^\]]+)\]\]", content)


def extract_tags(content: str) -> List[str]:
    """Find #tag tokens in content, excluding markdown headings."""
    return re.findall(r"(?<![\w#])#([\w\-]+)", content)


def graph_data() -> Dict:
    """Build nodes and links for graph view."""
    notes = list_notes()
    nodes = []
    links = []
    path_to_id = {}

    for i, note in enumerate(notes):
        path_to_id[note.path] = i
        nodes.append(
            {
                "id": i,
                "path": note.path,
                "title": note.title,
                "group": Path(note.path).parts[0] if Path(note.path).parts else "root",
            }
        )

    for note in notes:
        content = read_note(note.path)
        source = path_to_id[note.path]
        for target_name in extract_wiki_links(content):
            target_path = None
            for n in notes:
                if n.title.lower() == target_name.lower() or n.path.endswith(
                    f"/{target_name}.md"
                ):
                    target_path = n.path
                    break
            if target_path and target_path in path_to_id:
                links.append({"source": source, "target": path_to_id[target_path]})

    return {"nodes": nodes, "links": links}


def backlinks(relative_path: str) -> List[dict]:
    """Find notes that link to this note."""
    target_path = _resolve_path(relative_path)
    target_title = _extract_title(target_path)
    target_stem = target_path.stem
    results = []
    for p in config.VAULT_ROOT.rglob("*.md"):
        if p == target_path:
            continue
        content = p.read_text(encoding="utf-8", errors="ignore")
        links = extract_wiki_links(content)
        if target_title in links or target_stem in links:
            rel = p.relative_to(config.VAULT_ROOT).as_posix()
            results.append({"path": rel, "title": _extract_title(p)})
    return results


def _extract_title(path: Path) -> str:
    """Extract title from first H1 or filename."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return path.stem.replace("-", " ").replace("_", " ").title()
