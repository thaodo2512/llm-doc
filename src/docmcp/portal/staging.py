"""Contained WRITE surface for the portal — the only module that maps a
browser-supplied logical path to a real path under STAGING_ROOT (``raw/``),
guaranteeing containment (rejects ``..``, NUL, absolute, symlink-escape). It mirrors
``DocStore.resolve`` on the write side: callers must ``resolve()`` first, then RBAC-check
the resolved logical path (resolve-then-check). See PLAN §6 (write-path traversal).
"""

from __future__ import annotations

import os
from pathlib import Path

from ..atomicio import atomic_write_bytes


class StagingError(Exception):
    """A requested write path is malformed or escapes STAGING_ROOT."""


class StagingStore:
    def __init__(self, root: Path):
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, logical_path: str) -> Path:
        """Map a logical path to a real path contained under STAGING_ROOT.

        Same algorithm as DocStore.resolve: input is relative to the root regardless of
        leading slashes; ``..`` and NUL are rejected outright; the resolved path is
        compared against the real root so symlink/absolute escapes are caught.
        """
        rel = logical_path.strip().lstrip("/")
        if "\x00" in rel:
            raise StagingError(logical_path)
        if ".." in rel.replace("\\", "/").split("/"):
            raise StagingError(logical_path)
        candidate = (self._root / rel).resolve()
        if candidate != self._root and not candidate.is_relative_to(self._root):
            raise StagingError(logical_path)
        return candidate

    def to_logical(self, fs_path: Path) -> str:
        return "/" + Path(fs_path).resolve().relative_to(self._root).as_posix()

    def write_atomic(self, logical_path: str, data: bytes) -> Path:
        target = self.resolve(logical_path)
        if target == self._root:
            raise StagingError("refusing to write the staging root itself")
        atomic_write_bytes(target, data)
        return target

    def is_file(self, logical_path: str) -> bool:
        try:
            return self.resolve(logical_path).is_file()
        except StagingError:
            return False

    def list_under(self, prefix: str) -> list[str]:
        """Logical paths of the (non-dot, non-symlink) files under a logical prefix."""
        base = self.resolve(prefix)
        out: list[str] = []
        if base.is_file():
            return [self.to_logical(base)]
        if base.is_dir():
            for p in sorted(base.rglob("*")):
                if not p.is_file() or p.is_symlink():
                    continue
                if any(part.startswith(".") for part in p.relative_to(self._root).parts):
                    continue
                out.append(self.to_logical(p))
        return out

    def delete(self, logical_path: str) -> bool:
        target = self.resolve(logical_path)
        if target == self._root or target.is_symlink() or not target.is_file():
            return False
        target.unlink()
        return True

    def move(self, src_logical: str, dst_logical: str) -> Path:
        src = self.resolve(src_logical)
        dst = self.resolve(dst_logical)
        if not src.is_file() or src.is_symlink():
            raise StagingError("source not found")
        if dst == self._root:
            raise StagingError("invalid destination")
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)  # atomic within the same filesystem
        return dst
