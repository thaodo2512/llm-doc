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


_QUIETED = False


def _quiet_third_party_logs() -> None:
    """Docling/RapidOCR/transformers are noisy: progress bars, INFO spam, and — worst —
    a full traceback logged for *every* file they can't open (e.g. an encrypted PDF). That
    traceback reads like a crash in the operator's log even though we catch the error and
    report it as a calm one-line skip. Quiet these to CRITICAL by default so the only
    failure signal is our friendly summary; DOCMCP_INGEST_DEBUG restores full verbosity.

    Idempotent + cheap, and called inside each worker process (logging config does not
    cross a spawn boundary), so it must not import the heavy libs — it only nudges env
    vars + named loggers, which is safe even where Docling isn't installed.
    """
    global _QUIETED
    if _QUIETED:
        return
    _QUIETED = True
    if os.environ.get("DOCMCP_INGEST_DEBUG"):
        return  # troubleshooting: leave every library at full volume
    import logging

    os.environ.setdefault("TQDM_DISABLE", "1")  # silence "Loading weights" progress bars
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

    class _DropAll(logging.Filter):
        # A filter survives a later setLevel(): RapidOCR re-raises its own logger to
        # INFO when OCR initializes (lazily, long after we run), but it never clears
        # filters — so this keeps the chatter out regardless of when it configures.
        def filter(self, record):  # noqa: A003 — logging.Filter API
            return False

    drop = _DropAll()
    for name in (
        "docling", "docling_core", "docling_parse", "docling_ibm_models",
        "RapidOCR", "rapidocr", "transformers", "huggingface_hub", "torch",
    ):
        logger = logging.getLogger(name)
        logger.setLevel(logging.CRITICAL)
        logger.addFilter(drop)


# Human-friendly classification of a per-file parse failure. The point is the
# operator-facing summary: an encrypted PDF or a corrupt download is a SKIP, not a
# crash, so it must read as a calm one-liner — never a raw traceback. The full
# error string is still kept (manifest + ingest-status.json) for debugging.
def _friendly_reason(exc_type: str, message: str) -> str:
    m = message.lower()
    if exc_type in {"PdfiumError", "PdfPasswordError"} or "password" in m or "encrypted" in m:
        return "password-protected or encrypted"
    if "git lfs" in m or "un-materialized" in m or "lfs pointer" in m:
        return "ingest models not installed (run 'git lfs pull', then rebuild the ingest image)"
    if any(s in m for s in (
        "corrupt", "not a valid", "cannot open", "unexpected end",
        "truncated", "damaged", "eof marker", "malformed",
    )):
        return "unreadable or corrupt file"
    if exc_type in {"ImportError", "ModuleNotFoundError"}:
        return "a parser dependency is missing for this file type"
    first = (message.strip().splitlines() or [""])[0]
    return first[:100] or exc_type


def _parse_source(source_str: str) -> dict:
    """Parse ONE file to curated Markdown, returning a small picklable outcome.

    Module-level + self-contained so it runs unchanged in a worker process. It
    catches its OWN errors so a single poison file is recorded as a calm "failed"
    outcome (with a friendly reason) instead of crashing the pool or spewing a
    traceback into the operator's log. ``parse_file`` returning ``None`` means the
    file is binary/unsupported — a deliberate skip, reported but not an error.
    """
    _quiet_third_party_logs()  # once per process, incl. spawned workers
    source = Path(source_str)
    try:
        parsed = parse_file(source)
    except Exception as exc:  # noqa: BLE001 — isolate one bad file from the run
        frames = traceback.extract_tb(exc.__traceback__)
        informative = [
            f for f in frames
            if "site-packages" in f.filename or "docmcp" in f.filename.replace("\\", "/")
        ]
        frame = (informative or frames or [None])[-1]
        where = f"  (at {frame.filename}:{frame.lineno})" if frame else ""
        if os.environ.get("DOCMCP_INGEST_DEBUG"):
            traceback.print_exc()
        exc_type = type(exc).__name__
        return {
            "source": source_str,
            "status": "failed",
            "error": f"{exc_type}: {exc}{where}",
            "reason": _friendly_reason(exc_type, str(exc)),
        }
    if parsed is None:
        return {"source": source_str, "status": "unsupported", "ext": source.suffix.lower()}
    return {
        "source": source_str,
        "status": "parsed",
        "type": parsed.type,
        "markdown": parsed.markdown,
        "curated_suffix": parsed.curated_suffix,
    }


def _parse_parallel(sources: list[str], workers: int) -> list[dict]:
    """Parse ``sources`` across up to ``workers`` processes, preserving order.

    Docling/tree-sitter are CPU-bound and not thread-safe, so we use processes —
    each worker lazily builds its own converter and reuses it across its files, so
    the (expensive) model load is paid once per worker, not once per file. Degrades
    safely: if a worker dies (OOM/segfault) the pool breaks, and the remaining files
    are finished in-process rather than lost. Any pool-setup failure (spawn/pickle)
    falls back to a full sequential parse.
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from concurrent.futures.process import BrokenProcessPool

    n = max(1, min(workers, len(sources)))
    total = len(sources)
    step = max(1, total // 10)
    results: dict[str, dict] = {}
    try:
        with ProcessPoolExecutor(max_workers=n) as ex:
            futures = {ex.submit(_parse_source, s): s for s in sources}
            try:
                for fut in as_completed(futures):
                    results[futures[fut]] = fut.result()
                    done = len(results)
                    if total >= 8 and (done % step == 0 or done == total):
                        print(f"[ingest] parsed {done}/{total} file(s)…", file=sys.stderr)
            except BrokenProcessPool:
                pass  # a worker died; finish whatever is left in-process below
    except Exception as exc:  # noqa: BLE001 — spawn/pickle/setup failure
        print(
            f"[ingest] parallel parse unavailable ({type(exc).__name__}: {exc}); "
            "running sequentially",
            file=sys.stderr,
        )
    missing = [s for s in sources if s not in results]
    if missing and len(missing) != len(sources):
        print(
            f"[ingest] a parser worker stopped early; finishing {len(missing)} "
            "file(s) sequentially",
            file=sys.stderr,
        )
    for s in missing:
        results[s] = _parse_source(s)
    return [results[s] for s in sources]


def _print_summary(
    *, indexed: int, processed: int, skipped: int, failed: int, unsupported: int,
    unsupported_by_ext: dict[str, int], failures: list[dict], duration: float,
) -> None:
    """The operator-facing wrap-up. One calm headline, then — only if relevant — a
    breakdown of what was skipped and why. Skips and per-file failures are NOT
    errors (the run completed), so they read as notices, never as tracebacks."""
    bits = []
    if processed:
        bits.append(f"{processed} new/changed")
    if skipped:
        bits.append(f"{skipped} unchanged")
    detail = f" ({', '.join(bits)})" if bits else ""
    print(f"[ingest] done in {duration:.1f}s — {indexed} document(s) indexed{detail}.")
    if unsupported:
        breakdown = ", ".join(
            f"{ext} ×{c}"
            for ext, c in sorted(unsupported_by_ext.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        print(
            f"[ingest] skipped {unsupported} file(s) we don't index "
            f"(binary or unsupported type): {breakdown}"
        )
    if failed:
        print(
            f"[ingest] {failed} file(s) could not be read and were skipped "
            "(this is not a failure — everything else indexed fine):"
        )
        shown = failures[:20]
        for item in shown:
            name = Path(item["source"]).name
            print(f"           • {name} — {item.get('reason') or 'could not be parsed'}")
        if failed > len(shown):
            print(f"           …and {failed - len(shown)} more (full list in ingest-status.json)")


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
    processed = skipped = failed = unsupported = 0
    failures: list[dict] = []
    unsupported_by_ext: dict[str, int] = {}
    started_at = time.time()

    # --- Plan: hash each source (cheap) and apply the manifest fast-path BEFORE the
    # expensive parse. An unchanged source whose curated output still exists is reused
    # as-is (no re-parse). An unchanged source that FAILED last run stays failed (don't
    # re-burn the parser on a poison file) unless --retry-failed/--full. Everything
    # else — new / changed / previously-failed-with-retry / curated-missing — is queued.
    to_parse: list[str] = []
    sha_by_source: dict[str, str] = {}
    root_by_source: dict[str, Path] = {}
    for source_dir in settings.source_dirs:
        root = Path(source_dir).expanduser()
        if not root.is_dir():
            print(f"[ingest] source dir missing, skipping: {root}", file=sys.stderr)
            continue
        for source in _iter_files(root):
            key = str(source)
            try:
                src_sha = _sha256_file(source)
            except OSError as exc:  # unreadable even to hash — record + move on
                failed += 1
                err = f"{type(exc).__name__}: {exc}"
                failures.append({"source": key, "error": err, "reason": "could not be read"})
                manifest[key] = {
                    "sha256": None, "curated_path": None, "type": None,
                    "status": "failed", "error": err, "reason": "could not be read",
                    "indexed_at": int(time.time()),
                }
                continue
            sha_by_source[key] = src_sha
            root_by_source[key] = root
            prior = previous.get(key)
            if prior and prior.get("sha256") == src_sha:
                if prior.get("status") == "failed":
                    if not retry_failed:
                        manifest[key] = prior
                        failed += 1
                        failures.append({
                            "source": key,
                            "error": prior.get("error") or "(unchanged; previously failed)",
                            "reason": prior.get("reason") or "previously failed",
                        })
                        continue
                    # retry_failed → fall through to re-parse
                else:
                    cl = prior.get("curated_path")
                    if cl and cl not in claimed and (settings.doc_root / cl.lstrip("/")).is_file():
                        # Reuse the prior curated path verbatim (the whole point: no
                        # re-parse). A disambiguated path can stay "sticky" if its
                        # collision partner later disappears — harmless; a full rebuild
                        # re-normalizes it.
                        claimed[cl] = key
                        manifest[key] = {**prior, "status": "indexed"}
                        skipped += 1
                        continue
            to_parse.append(key)

    # --- Parse: the expensive step (Docling/tree-sitter). Fan out across worker
    # processes when INGEST_WORKERS > 1; otherwise parse in-process (deterministic,
    # and what the test suite + any monkeypatching caller depend on). Order is
    # preserved either way so curated-path collision disambiguation stays deterministic.
    _quiet_third_party_logs()
    workers = max(1, settings.ingest_workers)
    if workers > 1 and len(to_parse) > 1:
        print(
            f"[ingest] parsing {len(to_parse)} file(s) across "
            f"{min(workers, len(to_parse))} worker(s)…",
            file=sys.stderr,
        )
        outcomes = _parse_parallel(to_parse, workers)
    else:
        total = len(to_parse)
        step = max(1, total // 10)
        outcomes = []
        for i, src in enumerate(to_parse, 1):
            outcomes.append(_parse_source(src))
            if total >= 8 and (i % step == 0 or i == total):
                print(f"[ingest] parsed {i}/{total} file(s)…", file=sys.stderr)

    # --- Commit: deterministic + sequential. Curated writes, manifest, counters.
    for outcome in outcomes:
        key = outcome["source"]
        source = Path(key)
        src_sha = sha_by_source.get(key)
        status = outcome["status"]
        if status == "unsupported":
            # A deliberate skip (binary blob / type we can't extract text from), NOT
            # a failure. Counted + reported, never written to the manifest — re-checking
            # it next run is cheap (an 8 KB sniff), unlike re-running Docling.
            unsupported += 1
            ext = outcome.get("ext") or "(no extension)"
            unsupported_by_ext[ext] = unsupported_by_ext.get(ext, 0) + 1
            continue
        if status == "failed":
            failed += 1
            err = outcome.get("error") or "parse failed"
            reason = outcome.get("reason") or "could not be parsed"
            failures.append({"source": key, "error": err, "reason": reason})
            manifest[key] = {
                "sha256": src_sha, "curated_path": None, "type": None,
                "status": "failed", "error": err, "reason": reason,
                "indexed_at": int(time.time()),
            }
            continue
        parsed = Parsed(outcome["type"], outcome["markdown"], outcome["curated_suffix"])
        root = root_by_source[key]
        curated_logical = _curated_logical(root, source, parsed)
        owner = claimed.get(curated_logical)
        if owner is not None and owner != key:
            curated_logical = _disambiguate(curated_logical, source)
            print(
                f"[ingest] curated-path collision; remapped {source} -> {curated_logical}",
                file=sys.stderr,
            )
        claimed[curated_logical] = key
        curated_fs = settings.doc_root / curated_logical.lstrip("/")
        curated_fs.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(curated_fs, parsed.markdown)
        manifest[key] = {
            "sha256": src_sha,
            "curated_path": curated_logical,
            "type": parsed.type,
            "status": "indexed",
            "error": None,
            "indexed_at": int(time.time()),
        }
        processed += 1

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
                "schema": 2,
                "started_at": int(started_at),
                "finished_at": int(finished),
                "duration_s": round(finished - started_at, 3),
                "processed": processed,
                "skipped": skipped,
                "failed": failed,
                "unsupported": unsupported,
                "skipped_unsupported": unsupported_by_ext,
                "indexed_count": len(entries),
                "workers": workers,
                "failures": failures[:200],  # cap so a pathological run can't bloat the file
            },
            indent=2,
            sort_keys=True,
        ),
    )
    _print_summary(
        indexed=len(entries), processed=processed, skipped=skipped, failed=failed,
        unsupported=unsupported, unsupported_by_ext=unsupported_by_ext,
        failures=failures, duration=finished - started_at,
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
