"""Ingestion: code via tree-sitter (light) + incremental skip/reprocess."""

from __future__ import annotations

from docmcp.docstore import DocStore
from docmcp.ingest.pipeline import run_ingest


def test_code_file_chunked_to_markdown(ingested):
    store = DocStore(ingested.doc_root)
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
