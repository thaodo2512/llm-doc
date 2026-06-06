"""Docling document conversion (PDF/HTML -> clean Markdown). Marked `docling`.

Run the full suite to exercise these, or skip during fast iteration with
`-m 'not docling'`. First run downloads Docling's layout models.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docmcp.docstore import DocStore
from docmcp.ingest.pipeline import run_ingest
from docmcp.search.ripgrep import RipgrepBackend

DOCS = Path(__file__).parent / "fixtures" / "docs"


@pytest.mark.docling
def test_docling_html_and_pdf_to_markdown(settings_factory, tmp_path):
    settings = settings_factory(tmp_path, source_dirs=[str(DOCS)])
    entries = run_ingest(settings, full=True)
    by_path = {entry.path: entry for entry in entries}

    assert by_path["/guide.html.md"].type == "html"
    assert by_path["/handbook.pdf.md"].type == "pdf"

    store = DocStore(settings.doc_root)
    html_md = store.read("/guide.html.md").content
    pdf_md = store.read("/handbook.pdf.md").content

    assert "Incident Response Guide" in html_md
    assert "escalation_policy" in html_md  # underscore preserved

    # Critical: Docling's underscore escaping is disabled so config keys stay
    # literally searchable.
    assert "rollout_strategy" in pdf_md
    assert "rollout\\_strategy" not in pdf_md

    # End-to-end: keyword search finds the underscore'd key in the converted PDF.
    hits = RipgrepBackend(settings).search("rollout_strategy", ["/"], 5)
    assert any(h.path == "/handbook.pdf.md" for h in hits)
