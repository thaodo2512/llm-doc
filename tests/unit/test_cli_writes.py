"""Unit tests for the `docmcp` CLI write verbs (token / token-rm / token-rotate /
group / group-rm) — the scope/TTL policy and, critically, the stdout contract:
`token` and `token-rotate` must print ONLY the bare token to stdout (notes to
stderr), because the console and `setup` capture stdout to get a usable token."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from docmcp.cli import main


def _tokens(tmp_path) -> dict:
    return json.loads((tmp_path / "tokens.json").read_text(encoding="utf-8"))


def _only_rec(tmp_path) -> dict:
    return next(iter(_tokens(tmp_path).values()))


# --------------------------------------------------------------------------- #
# the stdout contract — the single most important property to preserve
def test_token_prints_only_the_token_to_stdout(tmp_path, capsys):
    tf = tmp_path / "tokens.json"
    rc = main(["token", str(tf), "alice", "/public", "--comment", "hi"])
    assert rc == 0
    cap = capsys.readouterr()
    token = cap.out.strip()
    assert cap.out == token + "\n"  # EXACTLY the token on stdout — nothing else
    assert token.startswith("tok_alice_")
    assert ("expires" in cap.err) or ("non-expiring" in cap.err)  # notes go to stderr
    rec = _tokens(tmp_path)[token]
    assert rec["allowed_prefixes"] == ["/public"] and rec["comment"] == "hi"


def test_token_rotate_prints_only_the_token_to_stdout(tmp_path, capsys):
    tf = tmp_path / "tokens.json"
    main(["token", str(tf), "bob", "/b"])
    capsys.readouterr()
    rc = main(["token-rotate", str(tf), "bob"])
    assert rc == 0
    cap = capsys.readouterr()
    assert cap.out == cap.out.strip() + "\n" and cap.out.strip().startswith("tok_bob_")
    assert "rotated bob" in cap.err


# --------------------------------------------------------------------------- #
# scope policy
def test_token_all_grants_whole_corpus(tmp_path):
    tf = tmp_path / "tokens.json"
    assert main(["token", str(tf), "root", "--all"]) == 0
    assert _only_rec(tmp_path)["allowed_prefixes"] == ["/"]


def test_token_all_rejects_extra_scope(tmp_path, capsys):
    tf = tmp_path / "tokens.json"
    assert main(["token", str(tf), "root", "/public", "--all"]) == 2
    assert "use --all alone" in capsys.readouterr().err
    assert not tf.exists()  # nothing written on a policy rejection


def test_token_requires_a_scope(tmp_path, capsys):
    tf = tmp_path / "tokens.json"
    assert main(["token", str(tf), "nobody"]) == 2
    assert "a scope is required" in capsys.readouterr().err


def test_token_rejects_bare_slash_prefix(tmp_path, capsys):
    tf = tmp_path / "tokens.json"
    assert main(["token", str(tf), "x", "/"]) == 2
    assert "bare '/'" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# expiry policy
def test_token_default_ttl_is_expiring(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKEN_TTL", raising=False)
    tf = tmp_path / "tokens.json"
    main(["token", str(tf), "a", "/x"])
    assert "expires_at" in _only_rec(tmp_path)  # default 90d => expiring


def test_token_expires_never(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKEN_TTL", raising=False)
    tf = tmp_path / "tokens.json"
    main(["token", str(tf), "a", "/x", "--expires", "never"])
    assert "expires_at" not in _only_rec(tmp_path)


def test_token_expires_spec(tmp_path):
    tf = tmp_path / "tokens.json"
    before = int(time.time())
    main(["token", str(tf), "a", "/x", "--expires", "2h"])
    assert before + 7200 <= _only_rec(tmp_path)["expires_at"] <= int(time.time()) + 7200


def test_token_bad_expires_is_rejected(tmp_path, capsys):
    tf = tmp_path / "tokens.json"
    assert main(["token", str(tf), "a", "/x", "--expires", "5x"]) == 2
    assert "invalid --expires" in capsys.readouterr().err
    assert not tf.exists()


# --------------------------------------------------------------------------- #
# provenance, groups/writes, multi-call append-default safety
def test_token_by_records_provenance(tmp_path):
    tf = tmp_path / "tokens.json"
    main(["token", str(tf), "a", "/x", "--by", "ci-bot"])
    assert _only_rec(tmp_path)["created_by"] == "ci-bot"


def test_token_groups_and_writes_do_not_leak_across_calls(tmp_path):
    # Guards the argparse append-default gotcha: a second mint must not inherit the
    # first call's --group/--write (default is None, not a shared []).
    tf = tmp_path / "tokens.json"
    main(["token", str(tf), "a", "--group", "team", "--write", "/w"])
    main(["token", str(tf), "b", "/b"])
    recs = {r["user"]: r for r in _tokens(tmp_path).values()}
    assert recs["a"]["groups"] == ["team"] and recs["a"]["writable_prefixes"] == ["/w"]
    assert "groups" not in recs["b"] and "writable_prefixes" not in recs["b"]


# --------------------------------------------------------------------------- #
# token-rm / token-rotate failure exits
def test_token_rm_by_user_and_masks_output(tmp_path, capsys):
    tf = tmp_path / "tokens.json"
    main(["token", str(tf), "alice", "/a"])
    main(["token", str(tf), "alice", "/b"])
    capsys.readouterr()
    assert main(["token-rm", str(tf), "alice"]) == 0
    out = capsys.readouterr().out
    assert "…" in out and "tok_alice_" not in out  # masked, never the full secret
    assert _tokens(tmp_path) == {}


def test_token_rm_no_match_exits_1(tmp_path, capsys):
    tf = tmp_path / "tokens.json"
    main(["token", str(tf), "a", "/a"])
    assert main(["token-rm", str(tf), "ghost"]) == 1
    assert "no token or user matching" in capsys.readouterr().err


def test_token_rotate_no_user_exits_1(tmp_path, capsys):
    tf = tmp_path / "tokens.json"
    main(["token", str(tf), "a", "/a"])
    assert main(["token-rotate", str(tf), "ghost"]) == 1
    assert "no tokens for user" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# groups
def test_group_define_and_remove(tmp_path, capsys):
    gf = tmp_path / "groups.json"
    assert main(["group", str(gf), "team", "/team/a", "/team/b"]) == 0
    assert json.loads(gf.read_text())["team"] == ["/team/a", "/team/b"]
    assert main(["group-rm", str(gf), "team"]) == 0
    assert "removed group team" in capsys.readouterr().out
    assert main(["group-rm", str(gf), "team"]) == 0  # idempotent
    assert "no such group" in capsys.readouterr().out


def test_group_invalid_name_rejected(tmp_path, capsys):
    gf = tmp_path / "groups.json"
    assert main(["group", str(gf), "bad name!", "/x"]) == 2
    assert "group name must match" in capsys.readouterr().err
    assert not gf.exists()
