"""Ingestion: code via tree-sitter (light) + incremental skip/reprocess."""

from __future__ import annotations

import pytest

from docmcp.docstore import DocStore
from docmcp.ingest.pipeline import run_ingest


def _treesitter_symbols_available() -> bool:
    """True only if tree-sitter symbol extraction actually works here.

    The `parse` extra can be installed but ABI-mismatched, in which case the parser
    silently falls back to whole-file (no symbol headers). Skip the symbol test then,
    so the documented `pytest -m 'not docling'` run stays green; it still runs where
    `.[parse]` is correctly installed (CI, the Docker ingest image)."""
    try:
        from docmcp.ingest.rich_parsers import _extract_symbols

        return bool(_extract_symbols(b"def probe():\n    return 1\n", "python"))
    except Exception:
        return False


@pytest.mark.skipif(
    not _treesitter_symbols_available(),
    reason="tree-sitter symbol extraction unavailable/degraded — install .[parse] with a matching ABI",
)
def test_code_file_chunked_to_markdown(ingested):
    store = DocStore(ingested.doc_root, ingested.index_json)
    index = {entry.path: entry for entry in store.load_index()}
    assert "/team-fw/sample.py.md" in index
    assert index["/team-fw/sample.py.md"].type == "code"

    content = store.read("/team-fw/sample.py.md").content
    assert "## `render_widget`" in content  # symbol header (file · symbol)
    assert "## `Widget`" in content  # class kept whole (incl. its methods)
    assert "```python" in content


def test_incremental_skip_then_reprocess(settings_factory, tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    source = raw / "a.md"
    source.write_text("# A\nhello world\n")
    settings = settings_factory(tmp_path, source_dirs=[str(raw)])

    run_ingest(settings, full=True)
    curated = settings.doc_root / "a.md"
    assert curated.is_file()

    # Unchanged source on the next run must be skipped: tamper the curated file
    # and confirm it is left intact.
    curated.write_text("TAMPERED\n")
    run_ingest(settings)
    assert curated.read_text() == "TAMPERED\n"

    # Changing the source re-processes it.
    source.write_text("# A\ngoodbye\n")
    run_ingest(settings)
    assert "goodbye" in curated.read_text()
