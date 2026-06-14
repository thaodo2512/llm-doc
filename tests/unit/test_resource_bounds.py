"""Resource bounds: read_doc byte/line caps and search_docs limit clamping.

These guard against an authenticated caller exhausting server memory with a huge
read or an unbounded `limit` (security review, medium findings).
"""

from __future__ import annotations

from pathlib import Path

from docmcp.docstore import DocStore
from docmcp.tools import DocTools


def _store(tmp_path: Path) -> tuple[DocStore, Path]:
    root = tmp_path / "curated"
    root.mkdir()
    return DocStore(root), root


# -- read_doc bounds ----------------------------------------------------------
def test_read_caps_lines_and_flags_truncated(tmp_path):
    store, root = _store(tmp_path)
    (root / "big.md").write_text("\n".join(f"line {i}" for i in range(1, 101)) + "\n")
    doc = store.read("/big.md", max_lines=10, max_bytes=1_000_000)
    assert doc.truncated is True
    assert len(doc.content.splitlines()) == 10
    assert doc.total_lines == 100  # total still reflects the whole (in-budget) file


def test_read_caps_bytes(tmp_path):
    store, root = _store(tmp_path)
    (root / "wide.md").write_text("x" * 5000)  # single long line
    doc = store.read("/wide.md", max_lines=5000, max_bytes=1000)
    assert doc.truncated is True
    assert len(doc.content) <= 1000


def test_read_byte_cap_counts_bytes_not_chars(tmp_path):
    store, root = _store(tmp_path)
    (root / "emoji.md").write_text("😀" * 1000)  # 4 bytes each => 4000 bytes
    doc = store.read("/emoji.md", max_lines=5000, max_bytes=1000)
    assert doc.truncated is True
    assert len(doc.content.encode("utf-8")) <= 1000  # true BYTE bound, not chars


def test_read_small_file_returned_verbatim(tmp_path):
    store, root = _store(tmp_path)
    (root / "small.md").write_text("a\nb\nc\n")
    doc = store.read("/small.md")
    assert doc.truncated is False
    assert doc.content == "a\nb\nc\n"
    assert doc.total_lines == 3


def test_read_range_window_is_capped(tmp_path):
    store, root = _store(tmp_path)
    (root / "big.md").write_text("\n".join(str(i) for i in range(1, 101)) + "\n")
    doc = store.read("/big.md", 1, 100, max_lines=5, max_bytes=1_000_000)
    assert doc.truncated is True
    assert len(doc.content.splitlines()) == 5


def test_do_read_threads_settings_bounds(settings_factory, tmp_path):
    s = settings_factory(tmp_path, max_read_lines=1)
    from docmcp.ingest.pipeline import run_ingest

    run_ingest(s, full=True)
    doc = DocTools(s).do_read("/public/welcome.md", None, None, ["/"])
    assert doc.truncated is True
    assert len(doc.content.splitlines()) == 1


# -- search_docs limit clamp --------------------------------------------------
class _StubBackend:
    def __init__(self):
        self.seen: int | None = None

    def search(self, query, allowed_prefixes, limit):
        self.seen = limit
        return []


def test_search_limit_clamped_to_max(settings):
    tools = DocTools(settings)  # max_search_limit defaults to 50
    stub = _StubBackend()
    tools._search = stub  # bypass building a real backend
    tools.do_search("q", 10_000, ["/"])
    assert stub.seen == settings.max_search_limit


def test_search_limit_floor_is_one(settings):
    tools = DocTools(settings)
    stub = _StubBackend()
    tools._search = stub
    tools.do_search("q", -5, ["/"])
    assert stub.seen == 1


def test_search_limit_passthrough_when_in_range(settings):
    tools = DocTools(settings)
    stub = _StubBackend()
    tools._search = stub
    tools.do_search("q", 7, ["/"])
    assert stub.seen == 7


def test_search_limit_non_finite_falls_back(settings):
    # int(inf) raises OverflowError; the clamp must swallow it, not propagate.
    tools = DocTools(settings)
    stub = _StubBackend()
    tools._search = stub
    tools.do_search("q", float("inf"), ["/"])
    assert stub.seen == 10  # fallback default, then clamped
