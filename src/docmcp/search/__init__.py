"""Search backends (keyword via ripgrep/FTS5; optional vector via Qdrant)."""

from __future__ import annotations

from ..config import Settings
from .base import SearchBackend


def build_backend(settings: Settings) -> SearchBackend:
    """Select the keyword backend from SEARCH_BACKEND."""
    if settings.search_backend == "fts5":
        from .fts5 import Fts5Backend

        return Fts5Backend(settings)
    from .ripgrep import RipgrepBackend

    return RipgrepBackend(settings)
