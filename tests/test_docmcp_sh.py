"""Static regression guards for docmcp.sh logic that can't be imported (it runs inside
the container as heredoc Python). These lock the review-finding fixes so a later edit
can't silently drop them. They assert on the relevant function body only, so an unrelated
match elsewhere in the script won't mask a regression."""

from __future__ import annotations

import re
from pathlib import Path

SH = (Path(__file__).resolve().parents[1] / "docmcp.sh").read_text(encoding="utf-8")


def _body(name: str) -> str:
    """Return the text of a shell function `name() { ... }`, up to the next top-level
    function definition (cmd_*, _cron_*, or usage)."""
    start = re.search(rf"\n{re.escape(name)}\(\)\s*\{{", SH)
    assert start, f"{name} not found in docmcp.sh"
    rest = SH[start.end():]
    nxt = re.search(r"\n(?:cmd_[a-z_]+|_cron_[a-z]+|usage)\(\)\s*\{", rest)
    return rest[: nxt.start()] if nxt else rest


def test_rotate_preserves_writable_prefixes():
    # MEDIUM finding: token-rotate must carry the user's portal write scope, not just read.
    # Assert the FUNCTIONAL lines (collection + write-back), so a comment-only mention of
    # writable_prefixes cannot satisfy the guard.
    body = _body("cmd_token_rotate")
    assert 'r.get("writable_prefixes"' in body  # collected from each old token
    assert 'rec["writable_prefixes"] = writes' in body  # written onto the new token


def test_backup_includes_groups_json():
    # MEDIUM finding: groups.json is permission-critical and gitignored — must be backed up.
    body = _body("cmd_backup")
    assert "groups.json" in body


def test_backup_caddy_lookup_tolerates_no_volume():
    # MEDIUM follow-up: a no-match grep for the caddy_data volume must NOT abort backup
    # under `set -o pipefail` (plaintext/VPN installs have no TLS volume).
    body = _body("cmd_backup")
    assert "grep -E 'caddy_data$' | head -1 || true" in body


def test_build_and_serve_wire_the_vector_serving_image():
    # Query-time semantic_search runs IN the server process, but the slim server has no
    # embedder/qdrant-client — so vector serving uses a dedicated server-vector image.
    build = _body("cmd_build")
    assert "--target server-vector" in build  # built via docker build --target
    assert "SERVER_VECTOR_IMAGE" in build
    assert "check_lfs_models" in build  # the embedding model is Git-LFS vendored
    serve = _body("cmd_serve")
    assert "DOCS_MCP_IMAGE" in serve  # docs-mcp is swapped to the vector image when enabled
    assert "SERVER_VECTOR_IMAGE" in serve
    assert "wait_for_qdrant" in serve  # qdrant is ready before the server serves queries


def test_ingest_sizes_workers_from_vm_resources():
    # Parallel parse must be sized from the Docker VM's REAL cpu+ram (not the host's, and
    # not a blind min(cpu,4) that OOMs a small VM at best quality), and thread-pinned so N
    # workers don't oversubscribe the cores.
    body = _body("cmd_ingest")
    assert "INGEST_WORKERS" in body
    assert "'{{.NCPU}}'" in body and "'{{.MemTotal}}'" in body  # cpu + memory aware, from the VM
    assert "OMP_NUM_THREADS" in body  # per-worker thread pin


def test_doctor_treats_per_file_skips_as_a_note_not_a_failure():
    # A few unreadable/unsupported files (encrypted PDF, binaries) must NOT mark the whole
    # deploy unhealthy — they're reported as a note. Only a truly empty index is a FAIL
    # (systemic model breakage is caught separately by the models/ingest-image checks).
    body = _body("cmd_doctor")
    assert "unreadable" in body and "skipped (unsupported type)" in body
    assert "if n == 0:" in body  # the only ingest-result condition that still FAILs the index


def test_doctor_fails_on_missing_or_empty_tokens():
    # MEDIUM follow-up: a missing/empty tokens.json means nobody can authenticate — the
    # verifier reports it as {} (0 tokens), so doctor must treat that as unhealthy.
    body = _body("cmd_doctor")
    assert '[ ! -f "$ROOT/tokens.json" ]' in body  # missing file is a FAIL, not a hollow PASS
    assert "if not v._digests" in body  # zero configured tokens is a FAIL


def test_doctor_validates_groups_and_portal():
    # LOW finding: doctor must parse tokens+groups via the real verifier and probe the portal.
    body = _body("cmd_doctor")
    assert "JsonFileTokenVerifier" in body  # tokens.json + groups.json schema via server code
    assert "/healthz" in body  # portal health probe
    assert "PORTAL_ENABLED" in body


def test_access_tree_renders_groups_users_and_write():
    # access-tree must surface groups (with folders), users, and the WRITE scope.
    body = _body("cmd_access_tree")
    assert "GROUPS" in body and "USERS" in body
    assert "effective_writable_prefixes" in body  # write scope shown, not just read
    assert "groups.json" in body  # resolves group folders + membership


def test_serve_portal_allows_tls_domain():
    # HIGH finding: the portal must start with a TLS DOMAIN, not only ALLOW_PLAINTEXT_PORTAL.
    body = _body("cmd_serve")
    assert "portal_tls=1" in body  # a real DOMAIN sets the TLS path
    assert "ALLOW_PLAINTEXT_PORTAL" in body  # plaintext remains an explicit alternative


def test_auth_mutations_reload_portal_not_just_server():
    # Stale single-file bind mount: tokens.json/groups.json are written atomically
    # (os.replace swaps the inode), so a single-file bind mount keeps reading the OLD file
    # until restarted. Both docs-mcp AND the portal must be restarted or the portal
    # authenticates a stale token set ("Invalid or expired token" on a fresh token).
    helper = _body("reload_auth_services")
    assert "dc restart docs-mcp" in helper
    assert "dc restart portal" in helper and "is_running portal" in helper
    # every command that mutates tokens.json / groups.json must route through the helper
    for cmd in ("cmd_token", "cmd_token_rm", "cmd_token_rotate", "cmd_group", "cmd_group_rm"):
        assert "reload_auth_services" in _body(cmd), cmd


def test_token_and_group_warn_on_unknown_read_prefix():
    # Non-blocking typo guard: a read/group prefix that matches NO indexed document is
    # almost always a typo (wrong case, partial segment). It must WARN and still proceed,
    # use the authoritative segment-aware is_allowed, and skip gracefully on an empty index.
    helper = _body("warn_unknown_prefixes")
    assert '[ "$#" -ge 1 ] || return 0' in helper  # never blocks; no scope → no-op
    assert "is_allowed" in helper  # authoritative, matches what the server enforces
    assert "__EMPTY__" in helper  # graceful skip when the index is unbuilt/empty
    assert "matches no document" in helper  # the warning text
    assert 'warn_unknown_prefixes "$@"' in _body("cmd_token")  # read positionals
    assert 'warn_unknown_prefixes "$@"' in _body("cmd_group")  # group's read prefixes
