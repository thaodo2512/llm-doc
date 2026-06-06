"""Ingestion orchestrator (build path), runnable as `docmcp-ingest`.

Walks SOURCE_DIRS, routes each file to curated Markdown, mirrors a clean tree
under DOC_ROOT, and regenerates the index. Incremental: a manifest stores each
source file's sha256 so unchanged files are skipped (important for the expensive
Docling/tree-sitter paths in M4).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path

from ..config import Settings
from ..types import IndexEntry
from . import indexer
from .parsers import Parsed, parse_file


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def _iter_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        # Skip dotfiles/dirs (e.g. .git) and the manifest itself.
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(root).parts):
            yield path


def _curated_logical(root: Path, source: Path, parsed: Parsed) -> str:
    """Deterministic curated logical path mirroring the source tree.

    md/text keep their stem + suffix; converted types append `.md` to the full
    filename so e.g. `foo.pdf` and `foo.docx` cannot collide.
    """
    rel_dir = source.parent.relative_to(root)
    if parsed.type in ("markdown", "text"):
        name = source.stem + parsed.curated_suffix
    else:
        name = source.name + ".md"
    return "/" + (rel_dir / name).as_posix()


def run_ingest(settings: Settings, *, full: bool = False) -> list[IndexEntry]:
    previous = {} if full else _load_manifest(settings.manifest_file)
    manifest: dict = {}
    settings.doc_root.mkdir(parents=True, exist_ok=True)
    processed = skipped = 0

    for source_dir in settings.source_dirs:
        root = Path(source_dir).expanduser()
        if not root.is_dir():
            print(f"[ingest] source dir missing, skipping: {root}", file=sys.stderr)
            continue
        for source in _iter_files(root):
            parsed = parse_file(source)
            if parsed is None:
                continue  # unsupported type
            src_sha = _sha256_file(source)
            curated_logical = _curated_logical(root, source, parsed)
            curated_fs = settings.doc_root / curated_logical.lstrip("/")
            prior = previous.get(str(source))
            if prior and prior.get("sha256") == src_sha and curated_fs.is_file():
                manifest[str(source)] = prior
                skipped += 1
                continue
            curated_fs.parent.mkdir(parents=True, exist_ok=True)
            curated_fs.write_text(parsed.markdown, encoding="utf-8")
            manifest[str(source)] = {
                "sha256": src_sha,
                "curated_path": curated_logical,
                "type": parsed.type,
            }
            processed += 1

    _save_manifest(settings.manifest_file, manifest)
    entries = indexer.build_entries(settings, manifest)
    indexer.write_index(settings, entries)
    if settings.search_backend == "fts5":
        from ..search.fts5 import build_fts5_index  # noqa: PLC0415 (lazy)

        build_fts5_index(settings, entries)
    print(
        f"[ingest] {processed} processed, {skipped} unchanged, "
        f"{len(entries)} indexed -> {settings.index_json}"
    )
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="docmcp-ingest", description="Build the curated doc store + index."
    )
    parser.add_argument(
        "--full", action="store_true", help="Ignore the manifest and reprocess everything."
    )
    parser.add_argument(
        "--source", action="append", metavar="DIR", help="Override SOURCE_DIRS (repeatable)."
    )
    args = parser.parse_args()
    settings = Settings.load()
    if args.source:
        settings = dataclasses.replace(settings, source_dirs=args.source)
    run_ingest(settings, full=args.full)


if __name__ == "__main__":
    main()
