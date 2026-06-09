"""Upload validation: an extension allowlist (keyed to what the ingest pipeline can
curate), a size cap, and Git-LFS-pointer rejection — applied BEFORE the file is written,
since it later becomes input to Docling/OCR. Conservative by design."""

from __future__ import annotations

import os

# Types the ingest pipeline curates: md/text passthrough, Docling (pdf/office/html),
# tree-sitter (code). Anything else would be silently dropped at ingest, so reject early.
ALLOWED_EXT = {
    ".md", ".markdown", ".txt", ".rst", ".csv", ".json", ".yaml", ".yml",
    ".html", ".htm", ".pdf", ".docx", ".pptx", ".doc", ".ppt",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".h",
    ".cpp", ".hpp", ".cc", ".rb", ".php", ".sh", ".sql", ".toml", ".ini",
}

_LFS_POINTER = b"git-lfs.github.com/spec"


def safe_filename(name: str) -> str:
    """Strip directory components and control chars from a client-supplied filename."""
    base = name.replace("\\", "/").split("/")[-1].strip()
    return "".join(ch for ch in base if ch >= " " and ch != "\x7f").lstrip(".") or ""


def has_allowed_ext(filename: str) -> bool:
    """True when the filename's extension is one the ingest pipeline can curate. Used to
    stop a rename from smuggling in an un-ingestable type that upload validation rejects."""
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXT


def validate_upload(filename: str, data: bytes, *, max_bytes: int) -> str | None:
    """Return a human error string if the upload is rejected, else None."""
    if not filename:
        return "missing filename"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return f"unsupported file type '{ext or '(none)'}'"
    if not data:
        return "empty file"
    if len(data) > max_bytes:
        return f"file too large ({len(data)} bytes > {max_bytes} limit)"
    if _LFS_POINTER in data[:512]:
        return "looks like an un-materialized Git-LFS pointer, not real content"
    return None
