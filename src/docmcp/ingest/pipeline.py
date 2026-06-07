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
import os
import sys
import traceback
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


_PROTECTED = {"index.json", "index.md", ".manifest.json"}


def _iter_files(root: Path) -> Iterator[Path]:
    real_root = root.resolve()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            continue  # don't follow symlinks out of the source tree (containment)
        if not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue  # dotfiles/dirs (e.g. .git)
        if not path.resolve().is_relative_to(real_root):
            continue  # reached via a symlinked parent dir
        yield path


def _curated_logical(root: Path, source: Path, parsed: Parsed) -> str:
    """Deterministic curated logical path mirroring the source tree.

    Converted types append `.md` to the full filename so e.g. `foo.pdf` and
    `foo.docx` cannot collide. md/text keep their name, but `foo.markdown` becomes
    `foo.markdown.md` so it cannot collide with `foo.md`.
    """
    rel_dir = source.parent.relative_to(root)
    if parsed.type in ("markdown", "text"):
        if source.suffix.lower() == parsed.curated_suffix:
            name = source.name
        else:
            name = source.name + parsed.curated_suffix
    else:
        name = source.name + ".md"
    return "/" + (rel_dir / name).as_posix()


def _disambiguate(curated_logical: str, source: Path) -> str:
    """Stable, source-derived disambiguator for curated-path collisions."""
    suffix = Path(curated_logical).suffix
    stem = curated_logical[: -len(suffix)] if suffix else curated_logical
    short = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{short}{suffix}"


def _sweep_orphans(settings: Settings, manifest: dict) -> None:
    """Delete curated docs whose source no longer exists (deletions/renames)."""
    expected = {
        (settings.doc_root / rec["curated_path"].lstrip("/")).resolve() for rec in manifest.values()
    }
    root = settings.doc_root.resolve()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name in _PROTECTED or path.suffix.lower().startswith(".sqlite"):
            continue
        if path.resolve() not in expected:
            path.unlink(missing_ok=True)
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            directory.rmdir()  # prune now-empty dirs
        except OSError:
            pass


def run_ingest(settings: Settings, *, full: bool = False) -> list[IndexEntry]:
    previous = {} if full else _load_manifest(settings.manifest_file)
    manifest: dict = {}
    claimed: dict[str, str] = {}  # curated_logical -> source, for collision detection
    settings.doc_root.mkdir(parents=True, exist_ok=True)
    processed = skipped = failed = 0

    for source_dir in settings.source_dirs:
        root = Path(source_dir).expanduser()
        if not root.is_dir():
            print(f"[ingest] source dir missing, skipping: {root}", file=sys.stderr)
            continue
        for source in _iter_files(root):
            try:
                parsed = parse_file(source)
                if parsed is None:
                    continue  # unsupported type
                curated_logical = _curated_logical(root, source, parsed)
                owner = claimed.get(curated_logical)
                if owner is not None and owner != str(source):
                    curated_logical = _disambiguate(curated_logical, source)
                    print(
                        f"[ingest] curated-path collision; remapped {source} -> {curated_logical}",
                        file=sys.stderr,
                    )
                claimed[curated_logical] = str(source)

                src_sha = _sha256_file(source)
                curated_fs = settings.doc_root / curated_logical.lstrip("/")
                prior = previous.get(str(source))
                if (
                    prior
                    and prior.get("sha256") == src_sha
                    and prior.get("curated_path") == curated_logical
                    and curated_fs.is_file()
                ):
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
            except Exception as exc:  # isolate one bad file from the whole run
                # Point at the deepest application/library frame (not stdlib
                # internals like json's decoder) so a cryptic error — e.g. a
                # JSONDecodeError from an un-materialized Git LFS model config —
                # names its caller instead of nowhere. DOCMCP_INGEST_DEBUG=1 prints
                # the full traceback.
                frames = traceback.extract_tb(exc.__traceback__)
                informative = [
                    f for f in frames
                    if "site-packages" in f.filename or "docmcp" in f.filename.replace("\\", "/")
                ]
                frame = (informative or frames or [None])[-1]
                where = f"  (at {frame.filename}:{frame.lineno})" if frame else ""
                print(
                    f"[ingest] failed to process {source}: {type(exc).__name__}: {exc}{where}",
                    file=sys.stderr,
                )
                if os.environ.get("DOCMCP_INGEST_DEBUG"):
                    traceback.print_exc()
                failed += 1

    _save_manifest(settings.manifest_file, manifest)
    _sweep_orphans(settings, manifest)  # remove curated docs whose source is gone
    entries = indexer.build_entries(settings, manifest)
    indexer.write_index(settings, entries)
    if settings.search_backend == "fts5":
        from ..search.fts5 import build_fts5_index  # noqa: PLC0415 (lazy)

        build_fts5_index(settings, entries)
    if settings.enable_vector:
        from ..search.vector import embed_and_upsert  # noqa: PLC0415 (lazy, optional deps)

        count = embed_and_upsert(settings, entries)
        print(f"[ingest] embedded {count} chunks -> Qdrant ({settings.qdrant_url})")
    print(
        f"[ingest] {processed} processed, {skipped} unchanged, {failed} failed, "
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
