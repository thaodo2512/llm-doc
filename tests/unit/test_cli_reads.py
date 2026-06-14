"""Unit tests for the read-only `docmcp` CLI verbs (token-list, group-list,
access-check, access-tree, audit). They reuse docmcp.console.reads, so this also
guards that the CLI and the console agree on RBAC views."""

from __future__ import annotations

import json

import pytest

from docmcp.cli import main


def _write(tmp_path, tokens, groups=None):
    (tmp_path / "tokens.json").write_text(json.dumps(tokens), encoding="utf-8")
    if groups is not None:
        (tmp_path / "groups.json").write_text(json.dumps(groups), encoding="utf-8")
    return str(tmp_path / "tokens.json")


def test_access_check_allow_deny_unknown_exit_codes(tmp_path, capsys):
    tf = _write(
        tmp_path,
        {
            "tok_alice_full": {"user": "alice", "allowed_prefixes": ["/"]},
            "tok_bob_public": {"user": "bob", "allowed_prefixes": ["/public"]},
        },
    )
    assert main(["access-check", tf, "alice", "/secret/x.md"]) == 0  # ALLOW: alice has "/"
    assert main(["access-check", tf, "bob", "/public/x.md"]) == 0  # ALLOW
    assert main(["access-check", tf, "bob", "/secret/x.md"]) == 1  # DENY (segment-aware)
    assert main(["access-check", tf, "carol", "/x"]) == 2  # UNKNOWN: no tokens
    out = capsys.readouterr().out
    assert "ALLOW" in out and "DENY" in out and "UNKNOWN" in out


def test_access_check_segment_aware_not_prefix_substring(tmp_path):
    # "/pub" must NOT grant "/public" — guards against a naive str.startswith bug.
    tf = _write(tmp_path, {"tok_x": {"user": "x", "allowed_prefixes": ["/pub"]}})
    assert main(["access-check", tf, "x", "/public/secret.md"]) == 1  # DENY


def test_token_list_renders_and_filters(tmp_path, capsys):
    tf = _write(
        tmp_path,
        {
            "tok_alice_aaaaaaaaaa": {"user": "alice", "allowed_prefixes": ["/"], "expires_at": None},
            "tok_bob_bbbbbbbbbb": {"user": "bob", "allowed_prefixes": ["/public"], "expires_at": 1},
        },
    )
    assert main(["token-list", tf]) == 0
    out = capsys.readouterr().out
    assert "alice" in out and "bob" in out and "EXPIRED" in out  # bob's epoch-1 token is expired

    assert main(["token-list", tf, "--user", "alice"]) == 0
    out = capsys.readouterr().out
    assert "alice" in out and "bob" not in out

    assert main(["token-list", tf, "--expired"]) == 0
    out = capsys.readouterr().out
    assert "bob" in out and "alice" not in out


def test_token_list_empty(tmp_path, capsys):
    tf = _write(tmp_path, {})
    assert main(["token-list", tf]) == 0
    assert "(no tokens)" in capsys.readouterr().out


def test_group_list_and_access_tree_expand_groups(tmp_path, capsys):
    tf = _write(
        tmp_path,
        {
            "tok_alice_full": {"user": "alice", "groups": ["team"]},
            "tok_bob_public": {"user": "bob", "allowed_prefixes": ["/public"]},
        },
        groups={"team": ["/team/a", "/team/b"]},
    )
    assert main(["group-list", tf]) == 0
    out = capsys.readouterr().out
    assert "team" in out and "/team/a" in out and "alice" in out  # alice is a member

    assert main(["access-tree", tf]) == 0
    out = capsys.readouterr().out
    assert "GROUPS" in out and "USERS" in out
    assert "/team/a" in out  # alice's read scope is group-derived
    assert "/public" in out  # bob's explicit scope


def test_audit_tail_last_n(tmp_path, capsys):
    log = tmp_path / "token-audit.jsonl"
    log.write_text(
        '{"event": "create", "user": "alice"}\n{"event": "revoke", "user": "bob"}\n',
        encoding="utf-8",
    )
    assert main(["audit", str(log), "-n", "1"]) == 0
    out = capsys.readouterr().out
    assert "revoke" in out and "create" not in out  # only the last record


def test_no_command_errors(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code != 0  # argparse: a subcommand is required
