"""PDF ingest pipeline — dependency-free (Docling mocked).

PDF conversion via Docling has regressed more than once (native libs, API drift),
and the real conversion test is gated behind `@pytest.mark.docling` (needs torch +
models). These tests stub the Docling converter so the *pipeline glue* — routing a
`.pdf` to type "pdf", the curated `foo.pdf.md` naming, failure isolation, and the
Markdown-export call — is verified on every test run, with no heavy deps, catching
the kind of signature/version drift that has broken PDF ingest before.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from docmcp.docstore import DocStore
from docmcp.ingest import rich_parsers
from docmcp.ingest.pipeline import run_ingest


class _FakeDocument:
    """Stands in for a Docling document. ``rejects`` names kwargs whose presence
    raises TypeError, mimicking a Docling version that lacks them."""

    def __init__(self, markdown: str, rejects: tuple[str, ...] = ()):
        self._markdown = markdown
        self._rejects = set(rejects)

    def export_to_markdown(self, **kwargs):
        bad = self._rejects & set(kwargs)
        if bad:
            raise TypeError(
                f"export_to_markdown() got an unexpected keyword argument {next(iter(bad))!r}"
            )
        return self._markdown


class _FakeConverter:
    def __init__(self, document=None, error: Exception | None = None):
        self._document = document
        self._error = error
        self.converted: list[str] = []

    def convert(self, path: str):
        self.converted.append(path)
        if self._error is not None:
            raise self._error
        return types.SimpleNamespace(document=self._document)


@pytest.fixture
def fake_docling(monkeypatch):
    """Make `.pdf` route through a stubbed Docling converter (no real docling install).

    `parse_rich` short-circuits to None when `import docling` fails, so we register a
    dummy `docling` module AND swap the cached converter factory.
    """

    def _install(converter):
        monkeypatch.setitem(sys.modules, "docling", types.ModuleType("docling"))
        monkeypatch.setattr(rich_parsers, "_CONVERTER", None, raising=False)
        monkeypatch.setattr(rich_parsers, "_converter", lambda: converter)
        return converter

    return _install


def test_pdf_ingests_to_curated_markdown(fake_docling, settings_factory, tmp_path):
    raw = tmp_path / "raw"
    (raw / "team-fw").mkdir(parents=True)
    (raw / "team-fw" / "handbook.pdf").write_bytes(b"%PDF-1.4 dummy bytes")
    fake_docling(_FakeConverter(_FakeDocument("# Handbook\n\nrollout_strategy = canary\n")))

    settings = settings_factory(tmp_path, source_dirs=[str(raw)])
    entries = run_ingest(settings, full=True)
    by_path = {e.path: e for e in entries}

    # .pdf -> curated foo.pdf.md, typed "pdf", content from the (stubbed) converter.
    assert "/team-fw/handbook.pdf.md" in by_path
    assert by_path["/team-fw/handbook.pdf.md"].type == "pdf"
    content = DocStore(settings.doc_root, settings.index_json).read(
        "/team-fw/handbook.pdf.md"
    ).content
    assert "rollout_strategy" in content


def test_pdf_export_survives_docling_kwarg_drift(fake_docling, settings_factory, tmp_path):
    """A Docling build whose export_to_markdown rejects escape_* kwargs must still
    ingest (this exact TypeError has failed every PDF in past regressions)."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "spec.pdf").write_bytes(b"%PDF-1.4 dummy")
    doc = _FakeDocument("# Spec\n\nkeep_me searchable\n", rejects=("escape_underscores", "escape_html"))
    fake_docling(_FakeConverter(doc))

    settings = settings_factory(tmp_path, source_dirs=[str(raw)])
    entries = run_ingest(settings, full=True)

    assert "/spec.pdf.md" in {e.path for e in entries}
    content = DocStore(settings.doc_root, settings.index_json).read("/spec.pdf.md").content
    assert "keep_me searchable" in content


def test_failed_pdf_is_isolated_not_fatal(fake_docling, settings_factory, tmp_path):
    """A PDF that blows up in Docling is recorded as failed but does NOT abort the
    run — sibling documents still get indexed (failure isolation)."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "broken.pdf").write_bytes(b"%PDF-1.4 broken")
    (raw / "ok.md").write_text("# OK\n\nplain markdown\n")
    fake_docling(_FakeConverter(error=RuntimeError("docling boom")))

    settings = settings_factory(tmp_path, source_dirs=[str(raw)])
    entries = run_ingest(settings, full=True)
    paths = {e.path for e in entries}

    # The good sibling is indexed; the broken PDF produced no curated doc.
    assert "/ok.md" in paths
    assert "/broken.pdf.md" not in paths

    # The failure is recorded in the manifest (visible via ingest-status.json too).
    import json

    manifest = json.loads(settings.manifest_file.read_text())
    broken = manifest[str(raw / "broken.pdf")]
    assert broken["status"] == "failed"
    assert "docling boom" in (broken["error"] or "")


def test_docling_jsondecodeerror_gives_actionable_message(fake_docling, settings_factory, tmp_path):
    """The real-world failure: Docling json.loads() an empty / LFS-pointer model file
    and raises 'JSONDecodeError: Expecting value: line 1 column 1 (char 0)' on every
    PDF. The recorded failure must name the fix (git lfs pull + rebuild), not the
    cryptic JSON error, and must not abort the run."""
    raw = tmp_path / "raw"
    (raw / "open_doc").mkdir(parents=True)
    (raw / "open_doc" / "DSP0248_1.3.0.pdf").write_bytes(b"%PDF-1.4 spec")
    (raw / "readme.md").write_text("# Readme\n\nstill indexes\n")
    # json.loads("") raises exactly the operator's error.
    boom = None
    try:
        json.loads("")
    except json.JSONDecodeError as exc:
        boom = exc
    fake_docling(_FakeConverter(error=boom))

    settings = settings_factory(tmp_path, source_dirs=[str(raw)])
    entries = run_ingest(settings, full=True)
    paths = {e.path for e in entries}

    assert "/readme.md" in paths  # the sibling still indexes — failure is isolated
    assert "/open_doc/DSP0248_1.3.0.pdf.md" not in paths

    manifest = json.loads(settings.manifest_file.read_text())
    err = manifest[str(raw / "open_doc" / "DSP0248_1.3.0.pdf")]["error"] or ""
    assert "git lfs pull" in err  # actionable, not just "Expecting value: line 1..."
    assert "RuntimeError" in err


def test_parse_document_translates_jsondecodeerror(monkeypatch):
    """Unit: _parse_document turns Docling's JSONDecodeError into a RuntimeError that
    points at the model/LFS fix."""

    class _Boom:
        def convert(self, _path):
            json.loads("")  # raises JSONDecodeError

    monkeypatch.setattr(rich_parsers, "_converter", lambda: _Boom())
    with pytest.raises(RuntimeError, match="git lfs pull"):
        rich_parsers._parse_document(__import__("pathlib").Path("/x/handbook.pdf"), "pdf")


def test_export_markdown_prefers_literal_underscores():
    """When the installed Docling accepts escape_underscores, we pass it as False so
    config keys stay literally searchable; the unit helper proves the contract."""
    captured = {}

    class _Doc:
        def export_to_markdown(self, escape_underscores=True, escape_html=True):
            captured["escape_underscores"] = escape_underscores
            captured["escape_html"] = escape_html
            return "ok"

    assert rich_parsers._export_markdown(_Doc()) == "ok"
    assert captured == {"escape_underscores": False, "escape_html": False}
