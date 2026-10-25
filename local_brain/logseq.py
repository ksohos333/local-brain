"""Write notes into the file-based Logseq vault.

The vault at ~/brain/logseq is Logseq's classic file-graph layout: a bullet
outline in markdown, one file per page, tabs for nesting. It is NOT the
DB-backed graph under ~/logseq/graphs, which the official `logseq` CLI manages.
Because the CLI can only adopt a *new* graph under its own root-dir (and
`graph import` would migrate the vault to sqlite), notes are written straight to
the markdown files instead. That keeps the files the source of truth.

Layout, per the vault's own logseq/config.edn:
    pages/<Title>.md
    journals/<yyyy_MM_dd>.md      page title renders as <yyyy-MM-dd>
"""

from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path

from . import config

# Appending to a page while another process (the Logseq app) may also write to
# it has to be read-modify-write under a lock, or blocks interleave.
_APPEND_LOCK = threading.Lock()

# Logseq's tag grammar: no spaces, no commas, no dashes at the edge.
_TAG_RE = re.compile(r"^[\w][\w\-/]*$", re.UNICODE)

# A page title becomes a filename, so the separators Logseq would otherwise use
# for hierarchy have to be neutralised. Logseq writes nested pages as
# `parent__child.md`, so reuse that rather than inventing a separator.
_ILLEGAL_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class LogseqError(RuntimeError):
    """A note could not be written to the vault."""


def vault_root() -> Path:
    root = Path(config.CONFIG["logseq"]["vault"]).expanduser()
    return root


def _pages_dir() -> Path:
    return vault_root() / "pages"


def _journals_dir() -> Path:
    return vault_root() / "journals"


def _journal_date_format() -> str:
    return config.CONFIG["logseq"].get("journal_date_format") or "%Y-%m-%d"


def journal_title(now: datetime | None = None) -> str:
    """Today's journal page title, matching the vault's date formatter."""
    return (now or datetime.now()).strftime(_journal_date_format())


def journal_filename(now: datetime | None = None) -> str:
    """Journal filenames use underscores: 2026_10_02.md."""
    return (now or datetime.now()).strftime("%Y_%m_%d") + ".md"


def normalise_tags(tags: str | list[str] | None) -> list[str]:
    """Accept a comma/space separated string or a list; return clean tags."""
    if not tags:
        return []
    if isinstance(tags, str):
        raw = re.split(r"[,\s]+", tags.strip())
    else:
        raw = [str(t).strip() for t in tags]
    out: list[str] = []
    for raw_tag in raw:
        if not raw_tag:
            continue
        tag = raw_tag.lstrip("#").lower()
        if not _TAG_RE.match(tag):
            raise LogseqError(
                f"invalid tag '{raw_tag}': use letters, digits, '-', '_' or '/'"
            )
        if tag and tag not in out:
            out.append(tag)
    return out


def validate_title(title: str) -> str:
    """Reject titles that would break the markdown file they become."""
    title = (title or "").strip()
    if not title:
        raise LogseqError("a page needs a title")
    if title.startswith(("-", "*", "#", ">")):
        raise LogseqError("note title cannot start with '-', '*', '#' or '>'")
    if "\n" in title or "\t" in title:
        raise LogseqError("note title must be a single line")
    if ".." in title:
        raise LogseqError("note title cannot contain '..'")
    return title


def page_filename(title: str) -> str:
    """Map a page title onto its markdown filename inside pages/."""
    cleaned = _ILLEGAL_FILENAME.sub("__", title)
    cleaned = re.sub(r"\.{2,}", ".", cleaned).strip().strip(".")
    if not cleaned:
        raise LogseqError("page title has no usable characters for a filename")
    if not cleaned.lower().endswith(".md"):
        cleaned += ".md"
    return cleaned


def _page_path(title: str) -> Path:
    base = _pages_dir()
    base.mkdir(parents=True, exist_ok=True)
    target = (base / page_filename(title)).resolve()
    # page_filename already strips separators, but assert it anyway: a bug here
    # would let a crafted title overwrite arbitrary files in the vault.
    if base.resolve() not in target.parents:
        raise LogseqError("page title resolves outside the vault")
    return target


def _journal_path(now: datetime | None = None) -> Path:
    base = _journals_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / journal_filename(now)


def _bullet(text: str, depth: int = 1) -> str:
    """One bullet at `depth`, Logseq-style: tab indent then '- '."""
    return f"{'\t' * depth}- {text}"


def _tag_suffix(tags: list[str]) -> str:
    return (" " + " ".join(f"#{t}" for t in tags)) if tags else ""


def status() -> dict:
    """Whether the vault looks usable, for the UI to show before saving."""
    root = vault_root()
    info = {
        "available": True,
        "vault": str(root),
        "reachable": False,
        "journal_title": journal_title(),
        "error": None,
    }
    if not root.is_dir():
        info["error"] = f"vault not found: {root}"
        return info
    try:
        root.joinpath("logseq", "config.edn").stat()
    except OSError:
        info["error"] = f"not a Logseq file vault (no logseq/config.edn): {root}"
        return info
    info["reachable"] = True
    return info


def create_note(
    kind: str,
    title: str = "",
    tags: str | list[str] | None = None,
    content: str = "",
) -> dict:
    """Create a page or append to today's journal.

    Returns {"page", "page_existed", "kind"}. For a page, `content` becomes the
    first block; for a journal, `title` is the block and `content` follows it.
    """
    if kind not in ("page", "journal"):
        raise LogseqError(f"kind must be 'page' or 'journal', got {kind!r}")

    clean_tags = normalise_tags(tags)
    content = (content or "").strip()

    if kind == "page":
        page = validate_title(title)
        body = content
        path = _page_path(page)
        existed = path.exists()
        lines = [_bullet(f"# {page}{_tag_suffix(clean_tags)}", depth=0)]
        if body:
            lines.append(_bullet(body, depth=1))
        # A page title is the first bullet; blocks are indented beneath it.
        with _APPEND_LOCK:
            if existed:
                with path.open("a", encoding="utf-8") as fh:
                    for line in lines[1:]:
                        fh.write(line + "\n")
            else:
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"page": page, "page_existed": existed, "kind": kind}

    # Journal: the note text is the top bullet, extra content nests beneath it.
    note_title = validate_title(title) if title.strip() else ""
    content = (content or "").strip()
    if not note_title and not content:
        raise LogseqError("a journal note needs some text")

    # With both fields filled the "Note" is the headline and Content is detail.
    if note_title:
        main_text = note_title
        children = [line for line in content.splitlines() if line.strip()]
    else:
        main_text = content
        children = []

    path = _journal_path()
    journal_page = journal_title()

    with _APPEND_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists()
        if new_file:
            path.write_text(_bullet(f"# {journal_page}", depth=0) + "\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            note_depth = 1
            fh.write(_bullet(f"{main_text}{_tag_suffix(clean_tags)}", depth=note_depth) + "\n")
            for child in children:
                fh.write(_bullet(child, depth=note_depth + 1) + "\n")

    return {"page": journal_page, "page_existed": not new_file, "kind": kind}