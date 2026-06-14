"""Unit tests for docmcp.tokenstore — the write-authority for tokens.json/groups.json.

The decisive checks round-trip a minted/rotated token through the REAL
JsonFileTokenVerifier (the production auth path), so a token this module writes is
proven to actually authenticate with the expected RBAC claims.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from docmcp import tokenstore
from docmcp.auth import JsonFileTokenVerifier


def _read(tokens_file: Path) -> dict:
    return json.loads(Path(tokens_file).read_text(encoding="utf-8"))


async def _claims(tokens_file, token, groups_file=None):
    verifier = JsonFileTokenVerifier(tokens_file, groups_file)
    access = await verifier.verify_token(token)
    return access.claims if access else None


# --------------------------------------------------------------------------- #
# mint
def test_mint_writes_record_and_returns_token(tmp_path):
    tf = tmp_path / "tokens.json"
    tok = tokenstore.mint(tf, "alice", prefixes=["/public", "/team/a"], created_by="ci")
    assert tok.startswith("tok_alice_")
    rec = _read(tf)[tok]
    assert rec["user"] == "alice"
    assert rec["allowed_prefixes"] == ["/public", "/team/a"]
    assert rec["created_by"] == "ci"
    assert isinstance(rec["created_at"], int)
    assert "expires_at" not in rec  # no ttl => non-expiring


async def test_mint_roundtrips_through_verifier(tmp_path):
    tf = tmp_path / "tokens.json"
    tok = tokenstore.mint(tf, "bob", prefixes=["/public"])
    claims = await _claims(tf, tok)
    assert claims is not None
    assert claims["user"] == "bob"
    assert claims["allowed_prefixes"] == ["/public"]
    assert claims["writable_prefixes"] == []
    # An unknown token must not authenticate.
    assert await _claims(tf, "tok_bob_deadbeefdeadbeefdeadbeef") is None


def test_mint_requires_a_scope(tmp_path):
    with pytest.raises(ValueError):
        tokenstore.mint(tmp_path / "tokens.json", "nobody")  # no prefixes/groups/writes


def test_mint_ttl_sets_expires_at(tmp_path):
    tf = tmp_path / "tokens.json"
    before = int(time.time())
    tok = tokenstore.mint(tf, "carol", prefixes=["/x"], ttl_seconds=3600)
    rec = _read(tf)[tok]
    assert before + 3600 <= rec["expires_at"] <= int(time.time()) + 3600


def test_mint_writes_and_comment_are_recorded(tmp_path):
    tf = tmp_path / "tokens.json"
    tok = tokenstore.mint(
        tf, "dora", prefixes=["/r"], writes=[" /w ", "", "/team "], comment="  ci bot  "
    )
    rec = _read(tf)[tok]
    # Stripped + empties dropped at write time (bash-faithful: not de-duped here —
    # effective_writable_prefixes de-dupes at read time, covered in test_auth).
    assert rec["writable_prefixes"] == ["/w", "/team"]
    assert rec["comment"] == "ci bot"  # stripped


async def test_mint_admin_all_grants_whole_corpus(tmp_path):
    tf = tmp_path / "tokens.json"
    tok = tokenstore.mint(tf, "root", prefixes=["/"])
    claims = await _claims(tf, tok)
    assert claims["allowed_prefixes"] == ["/"]


def test_tokens_file_is_0600(tmp_path):
    tf = tmp_path / "tokens.json"
    tokenstore.mint(tf, "alice", prefixes=["/x"])
    assert (os.stat(tf).st_mode & 0o777) == 0o600


def test_two_mints_accumulate(tmp_path):
    tf = tmp_path / "tokens.json"
    t1 = tokenstore.mint(tf, "a", prefixes=["/a"])
    t2 = tokenstore.mint(tf, "b", prefixes=["/b"])
    data = _read(tf)
    assert set(data) == {t1, t2}  # no lost update


# --------------------------------------------------------------------------- #
# revoke
def test_revoke_by_token_string(tmp_path):
    tf = tmp_path / "tokens.json"
    keep = tokenstore.mint(tf, "a", prefixes=["/a"])
    drop = tokenstore.mint(tf, "b", prefixes=["/b"])
    assert tokenstore.revoke(tf, drop) == [drop]
    assert set(_read(tf)) == {keep}


def test_revoke_by_user_removes_all_their_tokens(tmp_path):
    tf = tmp_path / "tokens.json"
    a1 = tokenstore.mint(tf, "alice", prefixes=["/a"])
    a2 = tokenstore.mint(tf, "alice", prefixes=["/b"])
    keep = tokenstore.mint(tf, "bob", prefixes=["/c"])
    removed = tokenstore.revoke(tf, "alice")
    assert set(removed) == {a1, a2}
    assert set(_read(tf)) == {keep}


def test_revoke_no_match_returns_empty(tmp_path):
    tf = tmp_path / "tokens.json"
    tokenstore.mint(tf, "a", prefixes=["/a"])
    assert tokenstore.revoke(tf, "ghost") == []


# --------------------------------------------------------------------------- #
# rotate
async def test_rotate_unions_scope_revokes_old_and_authenticates(tmp_path):
    tf = tmp_path / "tokens.json"
    old1 = tokenstore.mint(tf, "alice", prefixes=["/a"])
    old2 = tokenstore.mint(tf, "alice", prefixes=["/b"], writes=["/w"])
    new = tokenstore.rotate(tf, "alice", created_by="ci")
    data = _read(tf)
    assert set(data) == {new}  # old tokens gone, exactly one new
    assert old1 not in data and old2 not in data
    assert data[new]["last_rotated_at"] == data[new]["created_at"]
    claims = await _claims(tf, new)
    assert sorted(claims["allowed_prefixes"]) == ["/a", "/b"]  # union of read scope
    assert claims["writable_prefixes"] == ["/w"]


def test_rotate_expiry_style_all_expiring(tmp_path):
    tf = tmp_path / "tokens.json"
    tokenstore.mint(tf, "u", prefixes=["/a"], ttl_seconds=3600)
    tokenstore.mint(tf, "u", prefixes=["/b"], ttl_seconds=7200)
    new = tokenstore.rotate(tf, "u")
    assert "expires_at" in _read(tf)[new]  # every old token expired => new expires


def test_rotate_expiry_style_mixed_stays_non_expiring(tmp_path):
    tf = tmp_path / "tokens.json"
    tokenstore.mint(tf, "u", prefixes=["/a"], ttl_seconds=3600)
    tokenstore.mint(tf, "u", prefixes=["/b"])  # non-expiring
    new = tokenstore.rotate(tf, "u")
    assert "expires_at" not in _read(tf)[new]


def test_rotate_no_tokens_raises(tmp_path):
    tf = tmp_path / "tokens.json"
    tokenstore.mint(tf, "alice", prefixes=["/a"])
    with pytest.raises(LookupError):
        tokenstore.rotate(tf, "ghost")


# --------------------------------------------------------------------------- #
# groups
async def test_define_group_grants_members_via_verifier(tmp_path):
    tf = tmp_path / "tokens.json"
    gf = tmp_path / "groups.json"
    tokenstore.define_group(gf, "team", ["/team/a", "/team/b"])
    tok = tokenstore.mint(tf, "alice", groups=["team"])
    claims = await _claims(tf, tok, gf)
    assert sorted(claims["allowed_prefixes"]) == ["/team/a", "/team/b"]  # group-expanded


def test_remove_group_returns_false_when_absent(tmp_path):
    gf = tmp_path / "groups.json"
    tokenstore.define_group(gf, "team", ["/team/a"])
    assert tokenstore.remove_group(gf, "team") is True
    assert "team" not in json.loads(gf.read_text())
    assert tokenstore.remove_group(gf, "team") is False  # already gone


# --------------------------------------------------------------------------- #
# audit
def test_audit_log_records_create_revoke_rotate(tmp_path):
    tf = tmp_path / "tokens.json"
    tok = tokenstore.mint(tf, "alice", prefixes=["/a"], created_by="ci")
    tokenstore.revoke(tf, tok)
    tokenstore.mint(tf, "alice", prefixes=["/a"])
    tokenstore.rotate(tf, "alice")
    lines = [json.loads(x) for x in (tmp_path / "var" / "token-audit.jsonl").read_text().splitlines()]
    actions = [r["action"] for r in lines]
    assert actions == ["create", "revoke", "create", "rotate"]
    assert lines[0]["user"] == "alice" and lines[0]["by"] == "ci"
    assert "alice" in lines[1]["users"]  # revoke logs users, never the token
    assert lines[3]["replaced"] == 1
    # The audit never contains a token string.
    assert "tok_" not in (tmp_path / "var" / "token-audit.jsonl").read_text()
