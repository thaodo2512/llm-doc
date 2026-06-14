"""SearchBackend interface — one contract, swappable implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..types import Hit

MAX_SNIPPET = 240


class SearchBackend(ABC):
    @abstractmethod
    def search(self, query: str, allowed_prefixes: list[str], limit: int = 10) -> list[Hit]:
        """Return up to `limit` hits, restricted to `allowed_prefixes`."""
        raise NotImplementedError
