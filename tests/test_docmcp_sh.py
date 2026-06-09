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
