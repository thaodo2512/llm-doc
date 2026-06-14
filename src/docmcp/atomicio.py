"""Atomic file writes — write to a temp file in the same directory, fsync, then
``os.replace`` (an atomic rename on POSIX, Linux and macOS). A reader therefore
sees either the old complete file or the new complete file, never a half-written
one. Used for the index, manifest, status, and curated docs so a concurrent
reader (the server, or another ingest) never observes a truncated artifact.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so a rename into it survives a crash. Best-effort: silently
    skipped where unsupported (e.g. Windows has no O_DIRECTORY)."""
    try:
        fd = os.open(str(directory), getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def atomic_write_bytes(path: str | Path, data: bytes, *, mode: int | None = None) -> None:
    """Atomically write ``data`` to ``path`` (temp-in-same-dir + os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp in the *same directory* guarantees the same filesystem, so os.replace
    # is a true atomic rename rather than a cross-device copy.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())  # durably on disk before the rename
        if mode is not None:
            os.chmod(tmp, mode)  # set perms on the temp so the published file is never lax
        os.replace(tmp, path)
        _fsync_dir(path.parent)  # best-effort: make the rename itself crash-durable
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(
    path: str | Path, text: str, *, encoding: str = "utf-8", mode: int | None = None
) -> None:
    """Atomically write ``text`` to ``path``."""
    atomic_write_bytes(path, text.encode(encoding), mode=mode)
