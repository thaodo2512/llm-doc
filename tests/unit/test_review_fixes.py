"""Regression tests for issues found by the adversarial security review."""

from __future__ import annotations

import json

import pytest

from docmcp import rbac
from docmcp.config import Settings
from docmcp.docstore import DocStore, PathTraversalError
from docmcp.ingest.pipeline import run_ingest
from docmcp.tools import DocTools
from fastmcp.exceptions import ToolError


# --- #1 (HIGH): RBAC bypass via intra-root `..` ------------------------------


def test_read_dotdot_rbac_bypass_blocked(ingested):
    tools = DocTools(ingested)
    # A /public-scoped caller must NOT reach /team-fw by hopping through `..`.
    with pytest.raises(ToolError):
        tools.do_read("/public/../team-fw/design.md", None, None, ["/public"])


def test_rbac_norm_collapses_dotdot():
    assert rbac.is_allowed("/public/../team-fw/x.md", ["/public"]) is False
    assert rbac.is_allowed("/public/sub/../welcome.md", ["/public"]) is True


def test_resolve_rejects_dotdot(ingested):
    store = DocStore(ingested.doc_root)
    with pytest.raises(PathTraversalError):
        store.resolve("/public/../team-fw/design.md")


# --- #2 (HIGH): ripgrep scoping returns entitled in-prefix hits ---------------


def test_ripgrep_restricted_prefix_returns_entitled_hits(ingested):
    from docmcp.search.ripgrep import RipgrepBackend

    hits = RipgrepBackend(ingested).search("deploy_token", ["/public"], 10)
    assert any(h.path == "/public/welcome.md" for h in hits)  # not silently empty


# --- #3 (HIGH): deleted sources sweep orphaned curated docs -------------------


def test_orphan_sweep_removes_deleted_sources(settings_factory, tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "a.md").write_text("# A\nalpha\n")
    keep_b = raw / "b.md"
    keep_b.write_text("# B\nbeta\n")
    settings = settings_factory(tmp_path, source_dirs=[str(raw)])
    run_ingest(settings, full=True)
    assert (settings.doc_root / "a.md").is_file()

    (raw / "a.md").unlink()
    run_ingest(settings)
    assert (settings.doc_root / "b.md").is_file()
    assert not (settings.doc_root / "a.md").exists()  # orphan removed


# --- #4/#6: curated-path collisions are disambiguated, not overwritten --------


def test_md_and_markdown_do_not_collide(settings_factory, tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "guide.md").write_text("# Guide MD\n")
    (raw / "guide.markdown").write_text("# Guide MARKDOWN\n")
    settings = settings_factory(tmp_path, source_dirs=[str(raw)])
    entries = run_ingest(settings, full=True)
    paths = {e.path for e in entries}
    assert "/guide.md" in paths
    assert "/guide.markdown.md" in paths  # disambiguated, both survive


# --- #5: non-ASCII stored token must not break valid tokens -------------------


async def test_nonascii_stored_token_does_not_break_valid(tmp_path):
    from docmcp.auth import JsonFileTokenVerifier

    path = tmp_path / "tokens.json"
    path.write_text(
        json.dumps(
            {
                "tökèn_unicode": {"user": "x", "allowed_prefixes": ["/"]},
                "tok_good": {"user": "y", "allowed_prefixes": ["/"]},
            }
        )
    )
    verifier = JsonFileTokenVerifier(path)
    assert (await verifier.verify_token("tok_good")) is not None
    assert (await verifier.verify_token("nope")) is None


# --- #13: FTS5 NUL byte query must not crash ---------------------------------


def test_fts5_nul_byte_query_no_crash(ingested_fts):
    from docmcp.search.fts5 import Fts5Backend

    result = Fts5Backend(ingested_fts).search("deploy\x00token", ["/"], 5)
    assert isinstance(result, list)  # query-of-death neutralized


# --- #8: QDRANT_URL credentials redacted in settings dump --------------------


def test_qdrant_url_credentials_redacted(settings_factory, tmp_path):
    settings = settings_factory(tmp_path, qdrant_url="http://user:s3cret@qdrant:6333")
    redacted = settings.redacted()
    assert "s3cret" not in redacted["qdrant_url"]
    assert "***@qdrant:6333" in redacted["qdrant_url"]


# --- #15: invalid integer env vars fail fast ---------------------------------


def test_bind_port_rejects_garbage(monkeypatch):
    monkeypatch.setenv("BIND_PORT", "not-a-port")
    with pytest.raises(ValueError):
        Settings.load(dotenv=False)


def test_bind_port_rejects_out_of_range(monkeypatch):
    monkeypatch.setenv("BIND_PORT", "70000")
    with pytest.raises(ValueError):
        Settings.load(dotenv=False)


# --- #12: ambiguous/forbidden Origin headers are rejected --------------------


async def test_origin_middleware_rejects_multiple_origins():
    from docmcp.middleware import OriginValidationMiddleware

    called = {"app": False}

    async def app(scope, receive, send):
        called["app"] = True

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    mw = OriginValidationMiddleware(app, allowed_origins=["http://ok"])
    scope = {
        "type": "http",
        "headers": [(b"origin", b"http://ok"), (b"origin", b"http://evil")],
    }
    await mw(scope, receive, send)
    assert called["app"] is False
    assert sent and sent[0]["status"] == 403


async def test_origin_middleware_allows_no_origin():
    from docmcp.middleware import OriginValidationMiddleware

    called = {"app": False}

    async def app(scope, receive, send):
        called["app"] = True

    async def send(message):
        pass

    async def receive():
        return {"type": "http.request"}

    mw = OriginValidationMiddleware(app, allowed_origins=[])
    await mw({"type": "http", "headers": []}, receive, send)
    assert called["app"] is True  # CLI clients (no Origin) pass through
