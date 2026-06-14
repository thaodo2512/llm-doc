"""Search backends: result shape, RBAC scoping, ripgrep/FTS5 equivalence."""

from __future__ import annotations

from docmcp.search import build_backend
from docmcp.search.fts5 import Fts5Backend
from docmcp.search.ripgrep import RipgrepBackend
from docmcp.types import Hit


def test_ripgrep_hit_shape(ingested):
    backend = RipgrepBackend(ingested)
    hits = backend.search("deploy_token", ["/"], limit=10)
    assert hits and isinstance(hits[0], Hit)
    hit = next(h for h in hits if h.path == "/public/welcome.md")
    assert hit.line >= 1
    assert "deploy_token" in hit.snippet
    assert hit.score > 0


def test_fts5_hit_shape(ingested_fts):
    backend = Fts5Backend(ingested_fts)
    hits = backend.search("deploy_token", ["/"], limit=10)
    assert hits and isinstance(hits[0], Hit)
    hit = next(h for h in hits if h.path == "/public/welcome.md")
    assert hit.line >= 1
    assert "deploy_token" in hit.snippet


def test_backends_agree_on_target_doc(ingested, ingested_fts):
    rg_paths = {h.path for h in RipgrepBackend(ingested).search("retry_backoff", ["/"], 10)}
    fts_paths = {h.path for h in Fts5Backend(ingested_fts).search("retry_backoff", ["/"], 10)}
    assert "/team-fw/design.md" in rg_paths
    assert "/team-fw/design.md" in fts_paths


def test_ripgrep_respects_allowed_prefixes(ingested):
    backend = RipgrepBackend(ingested)
    # retry_backoff only appears under /team-fw -> a /public-scoped caller sees nothing.
    assert backend.search("retry_backoff", ["/public"], 10) == []


def test_fts5_respects_allowed_prefixes(ingested_fts):
    backend = Fts5Backend(ingested_fts)
    assert backend.search("retry_backoff", ["/public"], 10) == []


def test_build_backend_selects_implementation(ingested, ingested_fts):
    assert isinstance(build_backend(ingested), RipgrepBackend)
    assert isinstance(build_backend(ingested_fts), Fts5Backend)


def test_empty_query_returns_nothing(ingested):
    assert RipgrepBackend(ingested).search("", ["/"], 10) == []
