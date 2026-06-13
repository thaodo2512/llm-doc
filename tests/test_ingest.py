"""Ingestion: code via tree-sitter (light) + incremental skip/reprocess."""

from __future__ import annotations

import json

import pytest

from docmcp.docstore import DocStore
from docmcp.ingest import pipeline
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


def test_unsupported_binary_is_reported_not_failed(settings_factory, tmp_path):
    """A binary / unsupported file is SKIPPED and counted as 'unsupported' (so it can
    be reported to the operator), never indexed and never counted as a failure — the
    distinction the console needs to show a calm summary instead of scary errors."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "notes.md").write_text("# Notes\nhello\n")
    (raw / "archive.zip").write_bytes(b"PK\x03\x04\x00\x00binary\x00stuff")  # NUL ⇒ binary
    (raw / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42 binary")
    settings = settings_factory(tmp_path, source_dirs=[str(raw)])

    entries = run_ingest(settings, full=True)
    paths = {e.path for e in entries}
    assert "/notes.md" in paths  # the real doc indexed
    assert not any(p.endswith((".zip", ".mp4")) for p in paths)

    status = json.loads(settings.ingest_status_file.read_text())
    assert status["failed"] == 0  # NOT a failure
    assert status["unsupported"] == 2
    assert status["skipped_unsupported"] == {".zip": 1, ".mp4": 1}
    # unsupported files aren't recorded in the manifest (cheap to re-sniff next run).
    manifest = json.loads(settings.manifest_file.read_text())
    assert not any(k.endswith((".zip", ".mp4")) for k in manifest)


def test_failed_file_gets_friendly_reason(settings_factory, tmp_path, monkeypatch):
    """A supported file that errors (e.g. a password-protected PDF) is reported with a
    calm, human reason — not a raw traceback — while the full error is still recorded
    for debugging, and the run completes (the good sibling indexes)."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "secret.pdf").write_bytes(b"%PDF-1.4 enc")
    (raw / "ok.md").write_text("# OK\nfine\n")
    settings = settings_factory(tmp_path, source_dirs=[str(raw)])

    real = pipeline.parse_file

    class PdfiumError(Exception):  # mimics pypdfium2's error type name
        pass

    def fake_parse(path):
        if path.suffix == ".pdf":
            raise PdfiumError("Failed to load document (PDFium: Incorrect password error).")
        return real(path)

    monkeypatch.setattr(pipeline, "parse_file", fake_parse)
    run_ingest(settings, full=True)

    status = json.loads(settings.ingest_status_file.read_text())
    assert status["failed"] == 1
    failure = status["failures"][0]
    assert failure["reason"] == "password-protected or encrypted"
    assert "Incorrect password" in failure["error"]  # raw error kept for debugging
    paths = {e.path for e in DocStore(settings.doc_root, settings.index_json).load_index()}
    assert "/ok.md" in paths  # one bad file never aborts the run


def test_parallel_parse_matches_sequential(settings_factory, tmp_path):
    """INGEST_WORKERS>1 fans the parse across processes; the curated store, index, and
    skip counts must be byte-for-byte identical to the sequential path. This locks the
    invariant that the (order-preserving) commit phase is independent of worker count."""
    raw = tmp_path / "raw"
    raw.mkdir()
    for i in range(6):
        (raw / f"doc{i}.md").write_text(f"# Doc {i}\n\nbody {i}\n")
    (raw / "blob.bin").write_bytes(b"\x00\x01\x02 binary")  # unsupported

    seq = settings_factory(tmp_path / "seq", source_dirs=[str(raw)], ingest_workers=1)
    par = settings_factory(tmp_path / "par", source_dirs=[str(raw)], ingest_workers=4)
    e_seq = run_ingest(seq, full=True)
    e_par = run_ingest(par, full=True)

    assert {e.path for e in e_seq} == {e.path for e in e_par}
    for path in {e.path for e in e_seq}:
        a = (seq.doc_root / path.lstrip("/")).read_text()
        b = (par.doc_root / path.lstrip("/")).read_text()
        assert a == b  # identical curated content regardless of worker count

    s_seq = json.loads(seq.ingest_status_file.read_text())
    s_par = json.loads(par.ingest_status_file.read_text())
    assert s_seq["indexed_count"] == s_par["indexed_count"] == 6
    assert s_seq["unsupported"] == s_par["unsupported"] == 1
    assert s_par["workers"] == 4
