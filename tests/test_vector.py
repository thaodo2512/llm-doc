"""Optional vector layer.

- disabled (default): semantic_search errors and never touches Qdrant/OpenAI.
- enabled: real Qdrant exercised end-to-end with an injected deterministic
  embedder (so the vector machinery runs without an OpenAI key). Auto-skips if
  Qdrant isn't running.
"""

from __future__ import annotations

import hashlib
import math
import pathlib
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

    def __init__(self):
        self.prefixes_seen: list[str] = []  # records the `prefix` each embed() call got

    def embed(self, texts: list[str], *, prefix: str = "") -> list[list[float]]:
        self.prefixes_seen.append(prefix)
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


# --- offline-by-construction static guards (no deps; run everywhere) ---------


def test_offline_vector_stack_is_self_sufficient_and_no_openai():
    """The vendored model + offline deps must be baked in, and the offline default must
    NOT drag in openai — so an air-gapped build/runtime never reaches for the network."""
    root = pathlib.Path(__file__).resolve().parents[1]
    dockerfile = (root / "docker" / "Dockerfile").read_text()
    assert "AS server-vector" in dockerfile  # dedicated vector serving image
    assert "COPY models/bge-small-en-v1.5 /opt/models/embed" in dockerfile
    assert "HF_HUB_OFFLINE=1" in dockerfile  # defensive: never call the HF Hub

    pyproject = (root / "pyproject.toml").read_text()
    vector_block = re.search(r"\nvector = \[(.*?)\]", pyproject, re.S).group(1)
    assert "onnxruntime" in vector_block and "tokenizers" in vector_block  # offline embedder
    assert "openai" not in vector_block  # moved to the opt-in online extra
    assert "vector-openai" in pyproject  # legacy online backend is opt-in only


# --- embedder factory + offline local embedder ------------------------------


def test_make_embedder_selects_backend(settings_factory, tmp_path, monkeypatch):
    """The factory picks local vs openai purely from EMBED_BACKEND — no deps needed
    because we stub both embedder classes."""
    import docmcp.search.vector as vec

    monkeypatch.setattr(vec, "LocalOnnxEmbedder", lambda s: ("local", s))
    monkeypatch.setattr(vec, "OpenAIEmbedder", lambda s: ("openai", s))

    local = settings_factory(tmp_path, embed_backend="local")
    openai = settings_factory(tmp_path, embed_backend="openai")
    assert vec.make_embedder(local)[0] == "local"
    assert vec.make_embedder(openai)[0] == "openai"
    # default (unset → "local" via the Settings default) selects the offline path
    assert vec.make_embedder(settings_factory(tmp_path))[0] == "local"


def test_local_backend_never_imports_openai(settings_factory, tmp_path, monkeypatch):
    """The offline path must not require (or import) openai — air-gap safety."""
    import sys

    import docmcp.search.vector as vec

    monkeypatch.setattr(vec, "LocalOnnxEmbedder", lambda s: "local-ok")
    monkeypatch.setitem(sys.modules, "openai", None)  # any import of openai would now fail
    assert vec.make_embedder(settings_factory(tmp_path, embed_backend="local")) == "local-ok"


def test_local_onnx_embedder_pools_and_normalizes(settings_factory, tmp_path, monkeypatch):
    """LocalOnnxEmbedder math: feed the model's inputs, pool (CLS or attention-masked
    mean), L2-normalize. onnxruntime + tokenizers are faked; the math (numpy) is real."""
    np = pytest.importorskip("numpy")
    import sys
    import types

    captured: dict = {}

    class _Enc:
        ids = [101, 5, 102]
        attention_mask = [1, 1, 0]  # 3rd token masked out
        type_ids = [0, 0, 0]

    class _Tok:
        def enable_truncation(self, **k): ...
        def enable_padding(self, **k): ...
        def encode_batch(self, batch):
            captured["batch"] = list(batch)
            return [_Enc() for _ in batch]

    fake_tokenizers = types.ModuleType("tokenizers")
    fake_tokenizers.Tokenizer = types.SimpleNamespace(from_file=lambda p: _Tok())

    class _Sess:
        def __init__(self, path, providers=None): ...
        def get_inputs(self):
            return [types.SimpleNamespace(name=n) for n in ("input_ids", "attention_mask", "token_type_ids")]
        def run(self, outs, feeds):
            captured["fed"] = set(feeds)
            b, s = feeds["input_ids"].shape
            # token t -> vector [t+1, 1, 0, 0]; masking must drop the 3rd token from the mean
            h = np.zeros((b, s, 4), dtype=np.float32)
            for t in range(s):
                h[:, t, 0] = t + 1.0
                h[:, t, 1] = 1.0
            return [h]

    fake_ort = types.ModuleType("onnxruntime")
    fake_ort.InferenceSession = _Sess
    monkeypatch.setitem(sys.modules, "tokenizers", fake_tokenizers)
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)

    (tmp_path / "tokenizer.json").write_text("{}")
    (tmp_path / "model.onnx").write_bytes(b"x")

    from docmcp.search.vector import LocalOnnxEmbedder

    def _unit(v):
        a = np.array(v)
        return a / np.linalg.norm(a)

    # mean pooling: masked mean of tokens 0,1 = [(1+2)/2, 1, 0, 0] = [1.5, 1, 0, 0]
    mean_emb = LocalOnnxEmbedder(settings_factory(tmp_path, embed_model_dir=str(tmp_path), embed_dim=4, embed_pooling="mean"))
    v = mean_emb.embed(["hello"])[0]
    assert abs(np.linalg.norm(v) - 1.0) < 1e-5  # L2-normalized
    assert np.allclose(v, _unit([1.5, 1.0, 0.0, 0.0]), atol=1e-5)  # mask dropped token 2
    assert captured["fed"] == {"input_ids", "attention_mask", "token_type_ids"}

    # CLS pooling: token 0 only = [1, 1, 0, 0]
    cls_emb = LocalOnnxEmbedder(settings_factory(tmp_path, embed_model_dir=str(tmp_path), embed_dim=4, embed_pooling="cls"))
    assert np.allclose(cls_emb.embed(["hi"])[0], _unit([1.0, 1.0, 0.0, 0.0]), atol=1e-5)

    # the prefix is prepended before tokenization
    cls_emb.embed(["world"], prefix="Q: ")
    assert captured["batch"] == ["Q: world"]


def test_local_onnx_embedder_rejects_dim_mismatch(settings_factory, tmp_path, monkeypatch):
    """A mis-set EMBED_DIM is caught at construction (before it poisons the collection)."""
    pytest.importorskip("numpy")
    import sys
    import types

    np = sys.modules["numpy"]

    class _Tok:
        def enable_truncation(self, **k): ...
        def enable_padding(self, **k): ...
        def encode_batch(self, batch):
            return [types.SimpleNamespace(ids=[1], attention_mask=[1], type_ids=[0]) for _ in batch]

    fake_tok = types.ModuleType("tokenizers")
    fake_tok.Tokenizer = types.SimpleNamespace(from_file=lambda p: _Tok())

    class _Sess:
        def __init__(self, *a, **k): ...
        def get_inputs(self):
            return [types.SimpleNamespace(name="input_ids"), types.SimpleNamespace(name="attention_mask")]
        def run(self, outs, feeds):
            return [np.ones((1, 1, 8), dtype="float32")]  # model is 8-dim

    fake_ort = types.ModuleType("onnxruntime")
    fake_ort.InferenceSession = _Sess
    monkeypatch.setitem(sys.modules, "tokenizers", fake_tok)
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    (tmp_path / "tokenizer.json").write_text("{}")
    (tmp_path / "model.onnx").write_bytes(b"x")

    from docmcp.search.vector import LocalOnnxEmbedder

    with pytest.raises(ValueError, match="EMBED_DIM=4 but the model"):
        LocalOnnxEmbedder(settings_factory(tmp_path, embed_model_dir=str(tmp_path), embed_dim=4))  # model is 8-dim → mismatch


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


@pytest.mark.vector
@requires_qdrant
def test_prefixes_are_plumbed_to_embedder(ingested):
    """Passages embed with the passage prefix (ingest), the query with the query prefix
    (search) — the asymmetric-retrieval contract bge/e5 depend on."""
    import dataclasses

    from docmcp.docstore import DocStore
    from docmcp.search.vector import VectorSearch, embed_and_upsert

    s = dataclasses.replace(ingested, embed_query_prefix="Q: ", embed_passage_prefix="P: ")
    entries = DocStore(s.doc_root, s.index_json).load_index()
    fake = FakeEmbedder()
    embed_and_upsert(s, entries, embedder=fake, collection=TEST_COLLECTION)
    assert "P: " in fake.prefixes_seen  # passages got the passage prefix
    VectorSearch(s, embedder=fake, collection=TEST_COLLECTION).search("anything", ["/"], 3)
    assert "Q: " in fake.prefixes_seen  # query got the query prefix
