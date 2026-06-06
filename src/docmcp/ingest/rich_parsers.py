"""M4 parsers (optional `[parse]` deps): Docling docs + tree-sitter code.

Imported lazily by `parsers._parse_rich`, and the heavy libraries
(docling / tree-sitter) are imported only inside the functions that need them,
so a server-only install (no `[parse]`) still imports the ingest package.

- PDF / PPTX / DOCX / HTML -> Docling -> Markdown.
- Source code -> tree-sitter, chunked by top-level function/class (symbols kept
  whole) into fenced Markdown blocks with a `file · symbol` header. If a grammar
  or query is unavailable the whole file is emitted in one fenced block.
"""

from __future__ import annotations

from pathlib import Path

from .parsers import Parsed, normalize_text

DOCLING_EXTS = {
    ".pdf": "pdf",
    ".pptx": "pptx",
    ".docx": "docx",
    ".html": "html",
    ".htm": "html",
}

EXT_TO_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
}

# Capture the whole definition node (@def) and its name (@name). Node type names
# differ per grammar, so this is a per-language map; unlisted langs fall back to
# a whole-file block.
_QUERIES: dict[str, str] = {
    "python": """
        (function_definition name: (identifier) @name) @def
        (class_definition name: (identifier) @name) @def
    """,
    "javascript": """
        (function_declaration name: (identifier) @name) @def
        (class_declaration name: (identifier) @name) @def
    """,
    "typescript": """
        (function_declaration name: (identifier) @name) @def
        (class_declaration name: (type_identifier) @name) @def
        (interface_declaration name: (type_identifier) @name) @def
    """,
    "tsx": """
        (function_declaration name: (identifier) @name) @def
        (class_declaration name: (type_identifier) @name) @def
    """,
    "go": """
        (function_declaration name: (identifier) @name) @def
        (method_declaration name: (field_identifier) @name) @def
        (type_declaration (type_spec name: (type_identifier) @name)) @def
    """,
    "rust": """
        (function_item name: (identifier) @name) @def
        (struct_item name: (type_identifier) @name) @def
        (enum_item name: (type_identifier) @name) @def
        (trait_item name: (type_identifier) @name) @def
    """,
    "java": """
        (method_declaration name: (identifier) @name) @def
        (class_declaration name: (identifier) @name) @def
        (interface_declaration name: (identifier) @name) @def
    """,
    "ruby": """
        (method name: (identifier) @name) @def
        (class name: (constant) @name) @def
        (module name: (constant) @name) @def
    """,
    "c": """
        (function_definition
            declarator: (function_declarator declarator: (identifier) @name)) @def
    """,
    "cpp": """
        (function_definition
            declarator: (function_declarator declarator: (identifier) @name)) @def
        (class_specifier name: (type_identifier) @name) @def
    """,
}

_CONVERTER = None  # cached Docling DocumentConverter (expensive to build)


def parse_rich(path: Path) -> Parsed | None:
    ext = path.suffix.lower()
    if ext in DOCLING_EXTS:
        try:
            return _parse_document(path, DOCLING_EXTS[ext])
        except ImportError:
            return None  # docling not installed (server-only image)
    if ext in EXT_TO_LANG:
        return _parse_code(path, EXT_TO_LANG[ext])
    return None


# --- documents (Docling) -----------------------------------------------------


def _converter():
    global _CONVERTER
    if _CONVERTER is None:
        import os

        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        # OCR off by default: internal docs are typically born-digital, and OCR
        # pulls a large model and is slow. Enable with DOCLING_OCR=1 for scans.
        do_ocr = os.environ.get("DOCLING_OCR", "").strip().lower() in {"1", "true", "yes", "on"}
        pdf_opts = PdfPipelineOptions(do_ocr=do_ocr, do_table_structure=True)
        _CONVERTER = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_opts)}
        )
    return _CONVERTER


def _parse_document(path: Path, type_: str) -> Parsed:
    result = _converter().convert(str(path))
    # Disable markdown escaping so config keys / code symbols stay literally
    # searchable (e.g. "rollout_strategy" must not become "rollout\_strategy").
    markdown = result.document.export_to_markdown(escape_underscores=False, escape_html=False)
    return Parsed(type_, normalize_text(markdown), ".md")


# --- source code (tree-sitter) -----------------------------------------------


def _strictly_contains(outer, inner) -> bool:
    return (
        outer.start_byte <= inner.start_byte
        and inner.end_byte <= outer.end_byte
        and (outer.start_byte, outer.end_byte) != (inner.start_byte, inner.end_byte)
    )


def _extract_symbols(src: bytes, lang: str) -> list[dict]:
    query_src = _QUERIES.get(lang)
    if not query_src:
        return []
    # Use pip tree_sitter's Parser with language-pack's Language so the whole
    # path uses one consistent (property-based) API and parses bytes.
    from tree_sitter import Parser, Query, QueryCursor
    from tree_sitter_language_pack import get_language

    language = get_language(lang)
    tree = Parser(language).parse(src)
    captures = QueryCursor(Query(language, query_src)).captures(tree.root_node)

    defs = captures.get("def", [])
    names = captures.get("name", [])
    # Top-level only: a class def's text already includes its methods.
    outer = [d for d in defs if not any(_strictly_contains(o, d) for o in defs)]

    chunks: list[dict] = []
    for node in sorted(outer, key=lambda n: n.start_byte):
        # A class span contains its own name AND its methods' names; the
        # definition's own name is the earliest-starting one. (Picking "first
        # matching" is non-deterministic across tree-sitter capture orderings.)
        contained = [
            n for n in names if node.start_byte <= n.start_byte and n.end_byte <= node.end_byte
        ]
        name_node = min(contained, key=lambda n: n.start_byte) if contained else None
        chunks.append(
            {
                "name": name_node.text.decode("utf-8", "replace") if name_node else "<anonymous>",
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "text": src[node.start_byte : node.end_byte].decode("utf-8", "replace"),
            }
        )
    return chunks


def _fence(code: str, lang: str) -> str:
    code = code.rstrip("\n")
    longest = run = 0
    for ch in code:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{code}\n{ticks}"


def _parse_code(path: Path, lang: str) -> Parsed:
    src = path.read_bytes()
    try:
        chunks = _extract_symbols(src, lang)
    except Exception:  # grammar/ABI/query issue -> safe fallback
        chunks = []

    lines = [f"# `{path.name}`", ""]
    if chunks:
        for chunk in chunks:
            lines.append(
                f"## `{chunk['name']}`  (lines {chunk['start_line']}–{chunk['end_line']})"
            )
            lines += ["", _fence(chunk["text"], lang), ""]
    else:
        lines += [_fence(src.decode("utf-8", "replace"), lang), ""]
    return Parsed("code", normalize_text("\n".join(lines)), ".md")
