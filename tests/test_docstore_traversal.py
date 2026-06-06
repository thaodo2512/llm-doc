"""Path-traversal containment is the core security guarantee."""

from __future__ import annotations

import os

import pytest

from docmcp.docstore import DocStore, PathTraversalError


def test_resolve_normal_path(ingested):
    store = DocStore(ingested.doc_root)
    resolved = store.resolve("/public/welcome.md")
    assert resolved.is_file()
    assert resolved.is_relative_to(store.root)


@pytest.mark.parametrize(
    "evil",
    [
        "../../etc/passwd",
        "/../../etc/passwd",
        "/public/../../etc/passwd",
        "/public/../../../../etc/passwd",
        "/a/b/../../../outside",
    ],
)
def test_resolve_rejects_dotdot_escape(ingested, evil):
    store = DocStore(ingested.doc_root)
    with pytest.raises(PathTraversalError):
        store.resolve(evil)


def test_absolute_input_is_contained_not_escaped(ingested):
    # "/etc/passwd" is treated as a *logical* path under DOC_ROOT, not the real one.
    store = DocStore(ingested.doc_root)
    resolved = store.resolve("/etc/passwd")
    assert resolved.is_relative_to(store.root)
    assert not resolved.is_file()  # nothing there -> read() would 404, never the host's /etc/passwd


def test_symlink_escape_rejected(ingested, tmp_path):
    store = DocStore(ingested.doc_root)
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    link = ingested.doc_root / "escape.md"
    os.symlink(secret, link)
    with pytest.raises(PathTraversalError):
        store.resolve("/escape.md")


def test_read_line_range(ingested):
    store = DocStore(ingested.doc_root)
    full = store.read("/public/welcome.md")
    assert full.total_lines >= 3
    first = store.read("/public/welcome.md", 1, 1)
    assert first.total_lines == full.total_lines  # total reflects the whole file
    assert "Welcome" in first.content
