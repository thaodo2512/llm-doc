"""Ingestion orchestrator (build path), runnable as `docmcp-ingest`.

Walks SOURCE_DIRS, routes each file to curated Markdown, mirrors a clean tree
under DOC_ROOT, and regenerates the index. Incremental: a manifest stores each
source file's sha256 so unchanged files are skipped (important for the expensive
Docling/tree-sitter paths in M4).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import sys
import time
import traceback
from collections.abc import Iterator
from pathlib import Path

from ..atomicio import atomic_write_text
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
    atomic_write_text(path, json.dumps(manifest, indent=2, sort_keys=True))


# index.json / index.md / .manifest.json now live at the DOCSTORE ROOT, outside
# DOC_ROOT (see config + PLAN Appendix A.5), so they are no longer here to protect.
# An empty set means the orphan-sweep deletes any LEGACY in-curated copies left by
# an older build — a one-pass migration. The strict-subdir guard in Settings.load
# guarantees the live metadata can never be inside the swept tree.
_PROTECTED: set[str] = set()


@contextlib.contextmanager
def ingest_lock(lock_path: Path):
    """Cross-process exclusive lock so manual / cron / worker ingests never overlap.

    Uses ``fcntl.flock`` (portable across Linux — incl. inside containers — and
    macOS; the ``flock(1)`` CLI is Linux-only) on a lockfile shared via the docstore
    volume. Auto-releases when the fd closes / the process dies, so there is no stale
    lock to clean up.
    """
    import fcntl  # POSIX-only; imported lazily so importing this module stays portable

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise SystemExit(
            f"[ingest] another ingest is already running (lock: {lock_path})"
        ) from None
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


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
        (settings.doc_root / rec["curated_path"].lstrip("/")).resolve()
        for rec in manifest.values()
        if rec.get("curated_path")  # skip failed records (curated_path is None)
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


def run_ingest(
    settings: Settings, *, full: bool = False, retry_failed: bool = False, lock: bool = True
) -> list[IndexEntry]:
    """Build the curated store + index.

    Acquires the cross-process ingest lock so concurrent runs (manual / cron /
    worker / any direct Python caller) never overlap — not just the CLI entrypoint.
    Pass ``lock=False`` only if the caller already holds it. See PLAN Appendix A.4.
    """
    if lock:
        with ingest_lock(settings.ingest_lock_file):
            return _run_ingest(settings, full=full, retry_failed=retry_failed)
    return _run_ingest(settings, full=full, retry_failed=retry_failed)


def _run_ingest(
    settings: Settings, *, full: bool = False, retry_failed: bool = False
) -> list[IndexEntry]:
    previous = {} if full else _load_manifest(settings.manifest_file)
    manifest: dict = {}
    claimed: dict[str, str] = {}  # curated_logical -> source, for collision detection
    settings.doc_root.mkdir(parents=True, exist_ok=True)
    processed = skipped = failed = 0
    failures: list[dict] = []
    started_at = time.time()

    for source_dir in settings.source_dirs:
        root = Path(source_dir).expanduser()
        if not root.is_dir():
            print(f"[ingest] source dir missing, skipping: {root}", file=sys.stderr)
            continue
        for source in _iter_files(root):
            try:
                # Hash first (cheap) and check the manifest BEFORE the expensive
                # parse: an unchanged source whose curated output still exists and
                # whose path isn't contested this run is skipped WITHOUT re-running
                # Docling/tree-sitter. Falls through to parse for new / changed /
                # previously-failed / curated-missing / path-collision cases.
                src_sha = _sha256_file(source)
                prior = previous.get(str(source))
                if prior and prior.get("sha256") == src_sha:
                    if prior.get("status") == "failed":
                        if not retry_failed:
                            # Unchanged source that failed before: don't burn the
                            # parser on a poison file again. Keep it visible as failed;
                            # force a retry with --retry-failed (or --full).
                            manifest[str(source)] = prior
                            failed += 1
                            failures.append({
                                "source": str(source),
                                "error": prior.get("error") or "(unchanged; previously failed)",
                            })
                            continue
                        # retry_failed → fall through to re-parse
                    else:
                        cl = prior.get("curated_path")
                        if cl and cl not in claimed and (settings.doc_root / cl.lstrip("/")).is_file():
                            # Reuse the prior curated path verbatim (the whole point: no
                            # re-parse). A disambiguated path can stay "sticky" if its
                            # collision partner later disappears — harmless (still one
                            # valid doc); a full rebuild re-normalizes it.
                            claimed[cl] = str(source)
                            manifest[str(source)] = {**prior, "status": "indexed"}
                            skipped += 1
                            continue

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
                curated_fs = settings.doc_root / curated_logical.lstrip("/")
                curated_fs.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(curated_fs, parsed.markdown)
                manifest[str(source)] = {
                    "sha256": src_sha,
                    "curated_path": curated_logical,
                    "type": parsed.type,
                    "status": "indexed",
                    "error": None,
                    "indexed_at": int(time.time()),
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
                # Record the failure (visible via ingest-status.json / the manifest)
                # instead of only logging to stderr. No curated file is produced, so
                # the entry is skipped by the index + orphan-sweep. An UNCHANGED failed
                # source is NOT re-parsed next run (the fast path above keeps it failed)
                # unless --retry-failed/--full is passed or the source changes.
                err = f"{type(exc).__name__}: {exc}"
                failures.append({"source": str(source), "error": err})
                try:
                    failed_sha: str | None = _sha256_file(source)
                except OSError:
                    failed_sha = None
                manifest[str(source)] = {
                    "sha256": failed_sha,
                    "curated_path": None,
                    "type": None,
                    "status": "failed",
                    "error": err,
                    "indexed_at": int(time.time()),
                }

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
    finished = time.time()
    atomic_write_text(
        settings.ingest_status_file,
        json.dumps(
            {
                "schema": 1,
                "started_at": int(started_at),
                "finished_at": int(finished),
                "duration_s": round(finished - started_at, 3),
                "processed": processed,
                "skipped": skipped,
                "failed": failed,
                "indexed_count": len(entries),
                "failures": failures[:200],  # cap so a pathological run can't bloat the file
            },
            indent=2,
            sort_keys=True,
        ),
    )
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
        "--retry-failed", action="store_true",
        help="Re-parse sources that failed on a previous run (default: unchanged failures are skipped).",
    )
    parser.add_argument(
        "--source", action="append", metavar="DIR", help="Override SOURCE_DIRS (repeatable)."
    )
    args = parser.parse_args()
    settings = Settings.load()
    if args.source:
        settings = dataclasses.replace(settings, source_dirs=args.source)
    # run_ingest() acquires the ingest lock itself, so no wrapper lock here.
    run_ingest(settings, full=args.full, retry_failed=args.retry_failed)


if __name__ == "__main__":
    main()
