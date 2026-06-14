"""MCP tools.

The retrieval/RBAC logic lives in `DocTools` (plain, unit-testable methods that
take `allowed_prefixes` explicitly). The `@mcp.tool` wrappers are thin: they pull
the authenticated caller's claims via `get_access_token()` and delegate. Every
method intersects paths with the caller's allowed prefixes; `read_doc` *denies*
(raises) rather than silently returning empty.
"""

from __future__ import annotations

import time

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from . import obs, rbac
from .config import Settings
from .docstore import DocStore, PathTraversalError
from .types import DocContent, DocEntry, Hit


class DocTools:
    """Backend-agnostic implementation of the MCP tools."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = DocStore(settings.doc_root, settings.index_json)
        self._search = None  # lazily built keyword backend (M3)

    # -- list -----------------------------------------------------------------
    def do_list(self, path: str, allowed_prefixes: list[str]) -> list[DocEntry]:
        result: list[DocEntry] = []
        for entry in self.store.load_index():
            if not rbac.is_allowed(entry.path, allowed_prefixes):
                continue
            if path and not rbac.is_allowed(entry.path, [path]):
                continue
            result.append(entry.to_doc_entry())
        return result

    # -- read -----------------------------------------------------------------
    def do_read(
        self,
        path: str,
        start_line: int | None,
        end_line: int | None,
        allowed_prefixes: list[str],
    ) -> DocContent:
        # Canonicalize through the (containment-checked) resolver first, so RBAC is
        # evaluated on the real logical path rather than a `..`-laden alias.
        try:
            canonical = self.store.to_logical(self.store.resolve(path))
        except PathTraversalError:
            raise ToolError(f"Access denied: {path}") from None
        if not rbac.is_allowed(canonical, allowed_prefixes):
            raise ToolError(f"Access denied: {path} is outside your allowed prefixes.")
        try:
            return self.store.read(
                canonical,
                start_line,
                end_line,
                max_lines=self.settings.max_read_lines,
                max_bytes=self.settings.max_read_bytes,
            )
        except FileNotFoundError:
            raise ToolError(f"Not found: {path}") from None

    # -- search ---------------------------------------------------------------
    def _clamp_limit(self, limit: int) -> int:
        # An authenticated caller can ask for an arbitrarily large limit; clamp it
        # so one request can't fan out unboundedly.
        try:
            limit = int(limit)
        except (TypeError, ValueError, OverflowError):  # incl. inf/nan coercion
            limit = 10
        return max(1, min(limit, self.settings.max_search_limit))

    def do_search(self, query: str, limit: int, allowed_prefixes: list[str]) -> list[Hit]:
        backend = self._get_search_backend()
        return backend.search(query, allowed_prefixes, self._clamp_limit(limit))

    def _get_search_backend(self):
        if self._search is None:
            from .search import build_backend  # noqa: PLC0415 (lazy: M3)

            self._search = build_backend(self.settings)
        return self._search

    # -- semantic (optional) --------------------------------------------------
    def do_semantic_search(self, query: str, limit: int, allowed_prefixes: list[str]) -> list[Hit]:
        if not self.settings.enable_vector:
            raise ToolError(
                "semantic_search is disabled (ENABLE_VECTOR=false). Use search_docs instead."
            )
        from .search.vector import VectorSearch  # noqa: PLC0415 (lazy: optional deps)

        return VectorSearch(self.settings).search(query, allowed_prefixes, self._clamp_limit(limit))


def _caller() -> tuple[str, list[str]]:
    """The authenticated caller's (user_id, allowed_prefixes)."""
    token = get_access_token()
    if token is None:  # pragma: no cover - auth is enforced before tools run
        raise ToolError("Unauthorized")
    return token.claims["user"], list(token.claims["allowed_prefixes"])


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def register_tools(mcp: FastMCP, settings: Settings) -> DocTools:
    tools = DocTools(settings)

    @mcp.tool
    async def list_docs(path: str = "") -> list[DocEntry]:
        """List indexed documents under `path` (filtered to your allowed prefixes).

        Returns entries of {path, title, type, bytes, mtime}. Call this first to
        discover doc paths, then search_docs, then read_doc.
        """
        user, prefixes = _caller()
        t0 = time.perf_counter()
        result = tools.do_list(path, prefixes)
        obs.log_call(
            tool="list_docs", user=user, n_prefixes=len(prefixes),
            path=path, results=len(result), ms=_ms(t0),
        )
        return result

    @mcp.tool
    async def search_docs(query: str, limit: int = 10) -> list[Hit]:
        """Keyword/full-text search. Returns {path, line, snippet, score} hits
        restricted to your allowed prefixes. Use exact terms: code symbols,
        config keys, error strings. For meaning-based or paraphrased questions
        (when you can't name the exact term), use `semantic_search` instead, or as
        a fallback when this returns weak/no hits. `limit` is capped server-side."""
        user, prefixes = _caller()
        t0 = time.perf_counter()
        result = tools.do_search(query, limit, prefixes)
        obs.log_call(
            tool="search_docs", user=user, n_prefixes=len(prefixes),
            qlen=len(query or ""), results=len(result), ms=_ms(t0),
        )
        return result

    @mcp.tool
    async def read_doc(
        path: str, start_line: int | None = None, end_line: int | None = None
    ) -> DocContent:
        """Read a document (optionally a 1-based inclusive line range). Returns
        {path, content, total_lines, truncated}. Large reads are bounded; when
        `truncated` is true, request a narrower line range. Denied if `path` is
        outside your prefixes."""
        user, prefixes = _caller()
        t0 = time.perf_counter()
        try:
            result = tools.do_read(path, start_line, end_line, prefixes)
        except ToolError as exc:
            obs.log_call(
                tool="read_doc", user=user, n_prefixes=len(prefixes), path=path,
                denied=str(exc).startswith("Access denied"), error=type(exc).__name__, ms=_ms(t0),
            )
            raise
        obs.log_call(
            tool="read_doc", user=user, n_prefixes=len(prefixes), path=path,
            lines=result.total_lines, truncated=result.truncated, ms=_ms(t0),
        )
        return result

    @mcp.tool
    async def semantic_search(query: str, limit: int = 10) -> list[Hit]:
        """Semantic (vector) search — finds passages by MEANING, not exact words.
        Complements keyword `search_docs`: reach for it on conceptual or paraphrased
        questions, when you don't know the precise term, or as a fallback when
        `search_docs` returns weak or no hits. Returns the same
        {path, line, snippet, score} hits, restricted to your allowed prefixes.
        Optional and OFF by default: if it returns a "disabled" error, just fall
        back to `search_docs`. `limit` is capped server-side."""
        user, prefixes = _caller()
        t0 = time.perf_counter()
        try:
            result = tools.do_semantic_search(query, limit, prefixes)
        except ToolError as exc:
            obs.log_call(
                tool="semantic_search", user=user, n_prefixes=len(prefixes),
                error=type(exc).__name__, ms=_ms(t0),
            )
            raise
        obs.log_call(
            tool="semantic_search", user=user, n_prefixes=len(prefixes),
            qlen=len(query or ""), results=len(result), ms=_ms(t0),
        )
        return result

    return tools
