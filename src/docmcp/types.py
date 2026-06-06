"""Shared data models.

`IndexEntry` is the full internal record persisted to `index.json`. `DocEntry`,
`Hit`, and `DocContent` are the public shapes returned by the MCP tools (their
field sets are fixed by the brief, §7.2).
"""

from __future__ import annotations

from pydantic import BaseModel


class IndexEntry(BaseModel):
    """Full index record (a superset of DocEntry)."""

    path: str  # logical path under DOC_ROOT, e.g. "/public/foo.md"
    title: str
    type: str  # markdown | text | code | pdf | pptx | docx | html
    source_path: str  # original raw source the curated file came from
    bytes: int
    mtime: float
    sha256: str  # sha256 of the curated file's bytes

    def to_doc_entry(self) -> "DocEntry":
        return DocEntry(
            path=self.path, title=self.title, type=self.type, bytes=self.bytes, mtime=self.mtime
        )


class DocEntry(BaseModel):
    """Returned by `list_docs`."""

    path: str
    title: str
    type: str
    bytes: int
    mtime: float


class Hit(BaseModel):
    """Returned by `search_docs` / `semantic_search`."""

    path: str
    line: int
    snippet: str
    score: float


class DocContent(BaseModel):
    """Returned by `read_doc`."""

    path: str
    content: str
    total_lines: int
