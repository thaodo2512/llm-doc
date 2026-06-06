"""Keyword search via SQLite FTS5 (selectable alternative to ripgrep).

Indexed one row per non-blank line (`path, line_no, text`) so a match yields a
line number directly. Built during ingestion; queried read-only at request time.
The query is wrapped as a quoted FTS5 phrase (input escaped) and results are
scoped by a SQL prefix filter plus the shared RBAC post-filter.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import rbac
from ..config import Settings
from ..types import Hit, IndexEntry
from .base import SearchBackend

_SNIPPET = "snippet(doc_lines, 2, '[', ']', '…', 12)"


def build_fts5_index(settings: Settings, entries: list[IndexEntry]) -> None:
    """(Re)build the FTS5 database from curated docs (full rebuild for correctness)."""
    db_path = Path(settings.fts5_db).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DROP TABLE IF EXISTS doc_lines")
        conn.execute(
            "CREATE VIRTUAL TABLE doc_lines USING fts5(path UNINDEXED, line_no UNINDEXED, text)"
        )
        rows = []
        for entry in entries:
            fs = settings.doc_root / entry.path.lstrip("/")
            if not fs.is_file():
                continue
            text = fs.read_text(encoding="utf-8", errors="replace")
            for line_no, line in enumerate(text.splitlines(), start=1):
                if line.strip():
                    rows.append((entry.path, line_no, line))
        conn.executemany("INSERT INTO doc_lines(path, line_no, text) VALUES (?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def _fts_phrase(query: str) -> str:
    # Treat the whole query as a literal phrase; escape embedded quotes.
    return '"' + query.replace('"', '""') + '"'


def _prefix_filter(allowed_prefixes: list[str]) -> tuple[list[str], list[str]]:
    """SQL conditions + params restricting `path` to allowed prefixes.

    Returns ([], []) when unrestricted ("/" present).
    """
    conditions: list[str] = []
    params: list[str] = []
    for prefix in allowed_prefixes:
        norm = "/" + prefix.strip().strip("/")
        if norm == "/":
            return [], []  # unrestricted
        conditions.append("(path = ? OR path GLOB ?)")
        params += [norm, norm + "/*"]
    return conditions, params


class Fts5Backend(SearchBackend):
    def __init__(self, settings: Settings):
        self.db_path = Path(settings.fts5_db).expanduser()

    def search(self, query: str, allowed_prefixes: list[str], limit: int = 10) -> list[Hit]:
        query = (query or "").strip()
        if not query or not allowed_prefixes or not self.db_path.is_file():
            return []

        sql = (
            f"SELECT path, line_no, {_SNIPPET} AS snip, bm25(doc_lines) AS score "
            "FROM doc_lines WHERE doc_lines MATCH ?"
        )
        params: list = [_fts_phrase(query)]
        conditions, prefix_params = _prefix_filter(allowed_prefixes)
        if conditions:
            sql += " AND (" + " OR ".join(conditions) + ")"
            params += prefix_params
        sql += " ORDER BY score LIMIT ?"  # bm25 ascending = best first
        params.append(limit)

        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        hits: list[Hit] = []
        for path, line_no, snippet, score in rows:
            if not rbac.is_allowed(path, allowed_prefixes):  # defense in depth
                continue
            hits.append(Hit(path=path, line=int(line_no), snippet=snippet, score=-float(score)))
        return hits
