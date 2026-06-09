"""Optional vector layer.

- disabled (default): semantic_search errors and never touches Qdrant/OpenAI.
- enabled: real Qdrant exercised end-to-end with an injected deterministic
  embedder (so the vector machinery runs without an OpenAI key). Auto-skips if
  Qdrant isn't running.
"""

from __future__ import annotations

import hashlib
import math
import re
import urllib.request

import pytest

from docmcp.tools import DocTools
from fastmcp.exceptions import ToolError

QDRANT_URL = "http://localhost:6333"
TEST_COLLECTION = "docmcp_test"


class FakeEmbedder:
    """Deterministic bag-of-tokens vectors (process-independent hashing)."""

    dim = 256

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for token in re.findall(r"[A-Za-z0-9_]+", text.lower()):
                idx = int(hashlib.md5(token.encode()).hexdigest(), 16) % self.dim
                vec[idx] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


def _qdrant_running() -> bool:
    try:
        urllib.request.urlopen(QDRANT_URL + "/readyz", timeout=1)
        return True
    except Exception:
        return False


requires_qdrant = pytest.mark.skipif(not _qdrant_running(), reason="Qdrant not on :6333")


# --- disabled by default -----------------------------------------------------


def test_semantic_search_disabled_errors_without_touching_vector(settings, monkeypatch):
    import docmcp.search.vector as vector_module

    def _must_not_construct(*args, **kwargs):
        raise AssertionError("VectorSearch must not be built when ENABLE_VECTOR=false")

    monkeypatch.setattr(vector_module, "VectorSearch", _must_not_construct)
    tools = DocTools(settings)  # enable_vector defaults False
    with pytest.raises(ToolError):
        tools.do_semantic_search("anything", 5, ["/"])


def test_openai_embedder_wiring(settings, monkeypatch):
    """OpenAIEmbedder calls the API with the configured model (no network)."""
    pytest.importorskip("openai")  # the `vector` extra; skip cleanly if not installed
    import docmcp.search.vector as vector_module

    captured = {}

    class _FakeEmbeddings:
        def create(self, model, input):
            captured["model"] = model
            return type("R", (), {"data": [type("D", (), {"embedding": [0.1] * 1536})() for _ in input]})()

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self.embeddings = _FakeEmbeddings()

    monkeypatch.setattr("openai.OpenAI", _FakeClient)
    embedder = vector_module.OpenAIEmbedder(settings)
    vectors = embedder.embed(["hello", "world"])
    assert captured["model"] == settings.openai_embed_model
    assert len(vectors) == 2 and len(vectors[0]) == 1536


# --- enabled (real Qdrant) ---------------------------------------------------


@pytest.mark.vector
@requires_qdrant
def test_vector_search_enabled_returns_scoped_hits(ingested):
    from docmcp.docstore import DocStore
    from docmcp.search.vector import VectorSearch, embed_and_upsert

    entries = DocStore(ingested.doc_root, ingested.index_json).load_index()
    fake = FakeEmbedder()
    count = embed_and_upsert(ingested, entries, embedder=fake, collection=TEST_COLLECTION)
    assert count > 0

    search = VectorSearch(ingested, embedder=fake, collection=TEST_COLLECTION)

    hits = search.search("retry_backoff", ["/"], 5)
    assert hits and any(h.path == "/team-fw/design.md" for h in hits)
    assert all(h.line >= 1 and h.snippet for h in hits)

    # RBAC: a /public-scoped caller never sees /team-fw chunks (Qdrant filter + post-filter).
    scoped = search.search("retry_backoff", ["/public"], 5)
    assert all(h.path.startswith("/public") for h in scoped)
