"""Route a raw source file to curated Markdown.

M1 handles `.md`/`.txt` only. M4 extends `_parse_rich` with Docling
(PDF/PPTX/DOCX/HTML) and tree-sitter (source code), behind the optional
`[parse]` dependency group, so this module imports with base deps alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MARKDOWN_EXTS = {".md", ".markdown"}
TEXT_EXTS = {".txt"}


@dataclass(frozen=True)
class Parsed:
    type: str  # markdown | text | code | pdf | pptx | docx | html
    markdown: str  # curated content written to the doc store
    curated_suffix: str  # suffix for md/text passthroughs (".md" / ".txt")


def normalize_text(raw: str) -> str:
    """Normalize newlines and trailing whitespace; ensure a single trailing newline."""
    unified = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in unified.split("\n")]
    text = "\n".join(lines).strip("\n")
    return text + "\n" if text else ""


def parse_file(path: Path) -> Parsed | None:
    """Return curated Markdown for `path`, or None if the type is unsupported."""
    ext = path.suffix.lower()
    if ext in MARKDOWN_EXTS:
        return Parsed("markdown", normalize_text(_read(path)), ".md")
    if ext in TEXT_EXTS:
        return Parsed("text", normalize_text(_read(path)), ".txt")
    return _parse_rich(path)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_rich(path: Path) -> Parsed | None:
    """PDF/PPTX/DOCX/HTML and source code. Implemented in M4 (optional deps)."""
    try:
        from .rich_parsers import parse_rich  # noqa: PLC0415 (lazy: optional deps)
    except ImportError:
        return None
    return parse_rich(path)
