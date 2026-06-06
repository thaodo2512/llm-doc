"""Doc store — the ONLY module that resolves filesystem paths.

Every read goes through `DocStore.resolve()`, which maps a logical path
("/public/foo.md") to a real file under DOC_ROOT and guarantees the result stays
inside DOC_ROOT (rejecting `..`, symlink escapes, and absolute paths). No other
module may touch the filesystem for doc content. This is the path-traversal
defense.
"""

from __future__ import annotations

import json
from pathlib import Path

from .types import DocContent, IndexEntry


class PathTraversalError(Exception):
    """Raised when a requested path resolves outside DOC_ROOT."""


class DocStore:
    def __init__(self, doc_root: Path):
        # Resolve once; all containment checks compare against this real root.
        self._root = Path(doc_root).expanduser().resolve()

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, logical_path: str) -> Path:
        """Map a logical path to a real file path, contained within DOC_ROOT.

        Treats the input as relative to DOC_ROOT regardless of leading slashes,
        so an absolute-looking input like "/etc/passwd" is contained as
        DOC_ROOT/etc/passwd rather than escaping. `..` and symlink escapes are
        rejected because we compare the *resolved* path against the real root.
        """
        rel = logical_path.strip().lstrip("/")
        if "\x00" in rel:
            raise PathTraversalError(logical_path)
        # Reject parent-traversal components outright. `..` never escapes DOC_ROOT
        # (the containment check below catches that), but an *intra-root* `..` such
        # as "/public/../secret" would desync the RBAC prefix check from the real
        # resolved path — so forbid it here at the single resolver.
        if ".." in rel.replace("\\", "/").split("/"):
            raise PathTraversalError(logical_path)
        candidate = (self._root / rel).resolve()
        if candidate != self._root and not candidate.is_relative_to(self._root):
            raise PathTraversalError(logical_path)
        return candidate

    def to_logical(self, fs_path: Path) -> str:
        """Inverse of resolve(): real path under DOC_ROOT -> logical path."""
        return "/" + Path(fs_path).resolve().relative_to(self._root).as_posix()

    def read(
        self,
        logical_path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> DocContent:
        """Read a doc, optionally a 1-based inclusive line range."""
        fs = self.resolve(logical_path)
        if not fs.is_file():
            raise FileNotFoundError(logical_path)
        text = fs.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)
        if start_line is None and end_line is None:
            content = text
        else:
            start = max(1, start_line or 1)
            end = min(total, end_line if end_line is not None else total)
            content = "\n".join(lines[start - 1 : end]) if start <= end else ""
        return DocContent(path=logical_path, content=content, total_lines=total)

    def load_index(self) -> list[IndexEntry]:
        """Load index.json (empty list if it does not exist yet)."""
        index_path = self._root / "index.json"
        if not index_path.is_file():
            return []
        raw = json.loads(index_path.read_text(encoding="utf-8"))
        return [IndexEntry.model_validate(item) for item in raw]
