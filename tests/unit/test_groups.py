"""RBAC groups — a token can reference group names; auth resolves the
effective read prefixes (explicit + group-expanded), reloading on either file."""

from __future__ import annotations

import json
import time

from docmcp.atomicio import atomic_write_text
from docmcp.auth import JsonFileTokenVerifier, effective_prefixes


def test_effective_prefixes_merges_and_dedups():
    groups = {"fw": ["/team-fw"], "pub": ["/public"]}
    rec = {"user": "a", "allowed_prefixes": ["/public"], "groups": ["fw", "pub"]}
    assert effective_prefixes(rec, groups) == ["/public", "/team-fw"]  # /public deduped


def test_effective_prefixes_unknown_group_contributes_nothing():
    assert effective_prefixes({"groups": ["nope"]}, {}) == []
    assert effective_prefixes({"allowed_prefixes": ["/x"]}, {}) == ["/x"]


async def test_token_with_group_resolves_to_group_prefixes(tmp_path):
    tok, grp = tmp_path / "tokens.json", tmp_path / "groups.json"
    atomic_write_text(grp, json.dumps({"fw": ["/team-fw"]}))
    atomic_write_text(tok, json.dumps({"tok_a": {"user": "a", "groups": ["fw"]}}))
    at = await JsonFileTokenVerifier(tok, grp).verify_token("tok_a")
    assert at is not None and at.claims["allowed_prefixes"] == ["/team-fw"]


async def test_group_edit_reloads_without_restart(tmp_path):
    tok, grp = tmp_path / "tokens.json", tmp_path / "groups.json"
    atomic_write_text(grp, json.dumps({"fw": ["/team-fw"]}))
    atomic_write_text(tok, json.dumps({"tok_a": {"user": "a", "groups": ["fw"]}}))
    v = JsonFileTokenVerifier(tok, grp)
    assert (await v.verify_token("tok_a")).claims["allowed_prefixes"] == ["/team-fw"]
    time.sleep(0.01)
    atomic_write_text(grp, json.dumps({"fw": ["/team-fw", "/team-fw-2"]}))  # grow the group
    assert (await v.verify_token("tok_a")).claims["allowed_prefixes"] == ["/team-fw", "/team-fw-2"]


async def test_flat_tokens_without_groups_still_work(tmp_path):
    tok = tmp_path / "tokens.json"
    atomic_write_text(tok, json.dumps({"tok_a": {"user": "a", "allowed_prefixes": ["/public"]}}))
    assert (await JsonFileTokenVerifier(tok).verify_token("tok_a")).claims["allowed_prefixes"] == [
        "/public"
    ]


# --- review fixes: groups can't grant whole-corpus; malformed input is inert ---
def test_group_cannot_grant_whole_corpus():
    assert effective_prefixes({"groups": ["all"]}, {"all": ["/"]}) == []
    assert effective_prefixes({"groups": ["all"]}, {"all": ["", "  ", "//"]}) == []
    assert effective_prefixes({"groups": ["m"]}, {"m": ["/", "/team-fw"]}) == ["/team-fw"]


def test_effective_prefixes_ignores_malformed_values():
    # a string group value (typo) must NOT char-split into ['/', 't', ...] (would grant all)
    assert effective_prefixes({"groups": ["fw"]}, {"fw": "/team-fw"}) == []
    # a string allowed_prefixes is ignored, not char-split
    assert effective_prefixes({"allowed_prefixes": "/team-fw"}, {}) == []
    # but an explicit "/" on a token (admin / --all) is still honored
    assert effective_prefixes({"allowed_prefixes": ["/"]}, {}) == ["/"]


def test_empty_files_are_treated_as_empty_config(tmp_path):
    tok, grp = tmp_path / "tokens.json", tmp_path / "groups.json"
    tok.write_text(json.dumps({"tok_a": {"user": "a", "allowed_prefixes": ["/public"]}}))
    grp.write_text("")  # 0-byte groups.json must NOT crash startup
    assert JsonFileTokenVerifier(tok, grp)._groups == {}
    (tmp_path / "t2.json").write_text("   ")  # whitespace-only tokens.json → empty
    assert JsonFileTokenVerifier(tmp_path / "t2.json", grp)._digests == []
