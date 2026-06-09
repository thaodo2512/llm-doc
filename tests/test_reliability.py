"""Reliability hardening tests:

- atomic file writes
- docstore layout: internal files outside DOC_ROOT, not read_doc-able
- ingest status records: per-file + run summary
- cross-process ingest lock
- live token reload on atomic token-file change
"""

from __future__ import annotations

import json
import time

import pytest
from fastmcp.exceptions import ToolError

from docmcp import obs
from docmcp.atomicio import atomic_write_text
from docmcp.auth import JsonFileTokenVerifier
from docmcp.config import Settings
from docmcp.docstore import DocStore
from docmcp.ingest import pipeline
from docmcp.ingest.pipeline import ingest_lock, run_ingest
from docmcp.tools import DocTools


# --------------------------------------------------------------------------- #
# A.1 — atomic writes
def test_atomic_write_replaces_and_leaves_no_temp(tmp_path):
    target = tmp_path / "f.json"
    atomic_write_text(target, "v1")
    atomic_write_text(target, "v2")
    assert target.read_text() == "v2"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f.json"]  # no leftover .tmp


def test_atomic_write_keeps_original_on_replace_error(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"
    atomic_write_text(target, "good")

    def boom(*_a, **_k):
        raise OSError("replace failed")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(target, "bad")
    assert target.read_text() == "good"  # original intact
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f.txt"]  # temp cleaned up


# --------------------------------------------------------------------------- #
# A.5 — docstore layout / DOCSTORE_ROOT
def test_docstore_root_defaults_to_doc_root_parent(monkeypatch, tmp_path):
    monkeypatch.setenv("DOC_ROOT", str(tmp_path / "store" / "curated"))
    monkeypatch.delenv("DOCSTORE_ROOT", raising=False)
    s = Settings.load(dotenv=False)
    assert s.docstore_root == tmp_path / "store"
    assert s.manifest_file == tmp_path / "store" / ".manifest.json"
    assert s.index_json == tmp_path / "store" / "index.json"
    assert s.ingest_status_file == tmp_path / "store" / "ingest-status.json"


def test_doc_root_must_be_strict_subdir(monkeypatch, tmp_path):
    monkeypatch.setenv("DOC_ROOT", str(tmp_path / "store"))
    monkeypatch.setenv("DOCSTORE_ROOT", str(tmp_path / "store"))  # equal → reject
    with pytest.raises(ValueError, match="strict subdirectory"):
        Settings.load(dotenv=False)


def test_internal_files_live_outside_doc_root(ingested):
    s = ingested
    assert s.manifest_file.is_file() and s.manifest_file.parent == s.docstore_root
    assert s.index_json.is_file() and s.index_json.parent == s.docstore_root
    assert s.ingest_status_file.is_file()
    # legacy locations inside the served tree must NOT exist
    assert not (s.doc_root / ".manifest.json").exists()
    assert not (s.doc_root / "index.json").exists()


def test_internal_files_not_readable_via_read_doc(ingested):
    """A whole-tree ('/') reader cannot read_doc the manifest/index — they are
    not under DOC_ROOT, so DocStore.resolve can't reach them."""
    tools = DocTools(ingested)
    for path in ("/.manifest.json", "/index.json", "/index.md"):
        with pytest.raises(ToolError):
            tools.do_read(path, None, None, ["/"])


# --------------------------------------------------------------------------- #
# A.3 — ingest status records
def test_manifest_and_status_record_results(ingested):
    manifest = json.loads(ingested.manifest_file.read_text())
    assert manifest
    assert all(rec.get("status") == "indexed" for rec in manifest.values())
    assert all("indexed_at" in rec for rec in manifest.values())

    status = json.loads(ingested.ingest_status_file.read_text())
    assert status["schema"] == 1
    assert status["failed"] == 0
    assert status["indexed_count"] == sum(
        1 for r in manifest.values() if r.get("status") == "indexed"
    )
    assert "duration_s" in status


def test_failed_source_is_recorded_not_indexed(settings_factory, tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "good.md").write_text("# Good\nhello\n")
    (raw / "bad.md").write_text("# Bad\n")
    settings = settings_factory(tmp_path, source_dirs=[str(raw)])

    real = pipeline.parse_file

    def fake_parse(path):
        if path.name == "bad.md":
            raise RuntimeError("boom parsing bad")
        return real(path)

    monkeypatch.setattr(pipeline, "parse_file", fake_parse)
    run_ingest(settings, full=True)

    manifest = json.loads(settings.manifest_file.read_text())
    bad_rec = manifest[str(raw / "bad.md")]
    assert bad_rec["status"] == "failed"
    assert "boom" in bad_rec["error"]
    assert bad_rec["curated_path"] is None

    paths = {e.path for e in DocStore(settings.doc_root, settings.index_json).load_index()}
    assert not any("bad.md" in p for p in paths)  # excluded from the index

    status = json.loads(settings.ingest_status_file.read_text())
    assert status["failed"] == 1
    assert any("bad.md" in f["source"] for f in status["failures"])


# --------------------------------------------------------------------------- #
# A.4 — cross-process ingest lock
def test_ingest_lock_is_exclusive(tmp_path):
    lock = tmp_path / ".ingest.lock"
    with ingest_lock(lock):
        with pytest.raises(SystemExit):
            with ingest_lock(lock):  # second holder while the first is held → busy
                pass
    # released after the outer context exits → re-acquirable
    with ingest_lock(lock):
        pass


# --------------------------------------------------------------------------- #
# A.2 — live token reload (atomic token-file change, no restart)
async def test_token_reload_revokes_and_adds_without_restart(tmp_path):
    tokfile = tmp_path / "tokens.json"
    atomic_write_text(
        tokfile, json.dumps({"tok_alice": {"user": "alice", "allowed_prefixes": ["/public"]}})
    )
    verifier = JsonFileTokenVerifier(tokfile)
    assert await verifier.verify_token("tok_alice") is not None
    assert await verifier.verify_token("tok_bob") is None

    time.sleep(0.01)  # ensure a distinct mtime
    atomic_write_text(
        tokfile, json.dumps({"tok_bob": {"user": "bob", "allowed_prefixes": ["/team"]}})
    )
    assert await verifier.verify_token("tok_bob") is not None  # new token live
    assert await verifier.verify_token("tok_alice") is None  # revoked, no restart


async def test_token_reload_keeps_last_good_on_corrupt_write(tmp_path):
    tokfile = tmp_path / "tokens.json"
    atomic_write_text(
        tokfile, json.dumps({"tok_alice": {"user": "alice", "allowed_prefixes": ["/public"]}})
    )
    verifier = JsonFileTokenVerifier(tokfile)
    assert await verifier.verify_token("tok_alice") is not None

    time.sleep(0.01)
    tokfile.write_text("{ this is not valid json")  # corrupt (non-atomic) write
    # reload fails to parse → keep the last-known-good set (never deny-all)
    assert await verifier.verify_token("tok_alice") is not None


# --------------------------------------------------------------------------- #
# Incremental ingest: unchanged files skip the expensive parse (not just the write)
def test_unchanged_file_skips_expensive_parse(settings_factory, tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "a.md").write_text("# A\nhello\n")
    settings = settings_factory(tmp_path, source_dirs=[str(raw)])
    run_ingest(settings, full=True)  # first pass parses everything

    calls = []
    real = pipeline.parse_file

    def counting(p):
        calls.append(p.name)
        return real(p)

    monkeypatch.setattr(pipeline, "parse_file", counting)

    run_ingest(settings)  # incremental: an unchanged source must NOT be parsed
    assert "a.md" not in calls

    (raw / "a.md").write_text("# A\nworld\n")  # change the source → must re-parse
    run_ingest(settings)
    assert "a.md" in calls


# --------------------------------------------------------------------------- #
# Observability: structured, secret-free access logging
def test_obs_log_is_structured_and_secret_free(capsys, monkeypatch):
    monkeypatch.setattr(obs, "_ENABLED", True)
    obs.log_call(
        tool="read_doc", user="alice", n_prefixes=2, path="/public/x.md",
        token="tok_secret", authorization="Bearer tok_secret", lines=10,
    )
    err = capsys.readouterr().err.strip()
    rec = json.loads(err)
    assert rec["tool"] == "read_doc" and rec["user"] == "alice"
    assert rec["n_prefixes"] == 2 and rec["lines"] == 10 and "ts" in rec
    assert "token" not in rec and "authorization" not in rec  # forbidden keys stripped
    assert "tok_secret" not in err  # the secret never reaches the log


def test_obs_respects_disable_flag(capsys, monkeypatch):
    monkeypatch.setattr(obs, "_ENABLED", False)
    obs.log_call(tool="list_docs", user="bob")
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------- #
# run_ingest acquires the ingest lock itself (not just the CLI entrypoint)
def test_run_ingest_acquires_the_lock(settings_factory, tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "a.md").write_text("# A\nhi\n")
    settings = settings_factory(tmp_path, source_dirs=[str(raw)])
    with ingest_lock(settings.ingest_lock_file):  # hold it as a "concurrent" run
        with pytest.raises(SystemExit):
            run_ingest(settings)  # must refuse — lock is held
    run_ingest(settings)  # released → succeeds


# --------------------------------------------------------------------------- #
# A source that failed before is NOT re-parsed while unchanged (poison-file guard)
def test_unchanged_failed_source_not_retried_unless_forced(settings_factory, tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "good.md").write_text("# Good\nok\n")
    (raw / "bad.md").write_text("# Bad\n")
    settings = settings_factory(tmp_path, source_dirs=[str(raw)])

    real = pipeline.parse_file
    calls = []

    def fake_parse(p):
        calls.append(p.name)
        if p.name == "bad.md":
            raise RuntimeError("boom parsing bad")
        return real(p)

    monkeypatch.setattr(pipeline, "parse_file", fake_parse)

    run_ingest(settings, full=True)  # bad.md fails and is recorded
    assert "bad.md" in calls

    calls.clear()
    run_ingest(settings)  # incremental: unchanged failure must NOT be re-parsed
    assert "bad.md" not in calls
    status = json.loads(settings.ingest_status_file.read_text())
    assert status["failed"] == 1  # still surfaced as failed

    calls.clear()
    run_ingest(settings, retry_failed=True)  # forced → re-parsed
    assert "bad.md" in calls
