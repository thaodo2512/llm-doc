"""The console command allowlist — the security perimeter. These are pure (no Docker):
they prove that argument injection is rejected BEFORE any argv is built, that each value
is validated allowlist-style, and that builders emit exact argv lists."""

from __future__ import annotations

import os

import pytest

from docmcp.console import commands as c
from docmcp.console.commands import ValidationError

# Pin the repo root so the asserted argv is stable regardless of cwd.
os.environ.setdefault("DOCMCP_REPO_ROOT", "/repo")
SH = "/repo/docmcp.sh"


# --------------------------------------------------------------------------- #
# argument injection — every one of these must raise, never reach a spawn
@pytest.mark.parametrize(
    "user",
    ["--all", "-x", "a; rm -rf /", "a/b", "a b", "a\nb", "$(id)", "`id`", "a|b", "", "x" * 65],
)
def test_token_mint_rejects_bad_user(user):
    with pytest.raises(ValidationError):
        c.build("token.mint", user=user, prefixes=["/public"])


@pytest.mark.parametrize(
    "prefix",
    ["public", "--expires", "/a/../b", "/a/..", "/a;b", "/a$(x)", "/a\nb", "-/x"],
)
def test_token_mint_rejects_bad_prefix(prefix):
    with pytest.raises(ValidationError):
        c.build("token.mint", user="alice", prefixes=[prefix])


def test_bare_slash_prefix_rejected_for_read():
    with pytest.raises(ValidationError):
        c.build("token.mint", user="alice", prefixes=["/"])


def test_scope_required():
    with pytest.raises(ValidationError):
        c.build("token.mint", user="alice")  # no prefixes/groups/writes


def test_unknown_action_rejected():
    with pytest.raises(ValidationError):
        c.build("rm.rf", path="/")


# --------------------------------------------------------------------------- #
# exact argv for valid input
def test_token_mint_argv_exact():
    argv = c.build("token.mint", user="alice", prefixes=["/public", "/team/a"], expires="90d")
    assert argv == [SH, "token", "alice", "/public", "/team/a", "--expires", "90d"]


def test_token_mint_admin_argv():
    assert c.build("token.mint", user="admin", admin=True, expires="never") == [
        SH, "token", "admin", "--all", "--expires", "never",
    ]


def test_token_mint_groups_and_writes():
    argv = c.build("token.mint", user="bob", groups=["team"], writes=["/team/bob"])
    assert argv == [SH, "token", "bob", "--group", "team", "--write", "/team/bob"]


def test_admin_write_root_allowed():
    # writable '/' is an intentional admin break-glass (allow_root=True)
    argv = c.build("token.mint", user="admin", writes=["/"])
    assert argv == [SH, "token", "admin", "--write", "/"]


def test_group_define_and_remove():
    assert c.build("group.define", name="team", prefixes=["/team"]) == [SH, "group", "team", "/team"]
    assert c.build("group.remove", name="team") == [SH, "group-rm", "team"]
    with pytest.raises(ValidationError):
        c.build("group.define", name="bad name", prefixes=["/team"])
    with pytest.raises(ValidationError):
        c.build("group.define", name="team", prefixes=["/"])  # groups can't grant whole corpus


def test_lifecycle_argv():
    assert c.build("ingest") == [SH, "ingest"]
    assert c.build("ingest", full=True) == [SH, "ingest", "--full"]
    assert c.build("build", target="all") == [SH, "build", "all"]
    assert c.build("serve") == [SH, "serve"]
    assert c.build("backup") == [SH, "backup"]
    with pytest.raises(ValidationError):
        c.build("build", target="; rm")


# --------------------------------------------------------------------------- #
# scalar validators
def test_expires_validator():
    assert c.valid_expires("90d") == "90d"
    assert c.valid_expires("never") == "never"
    for bad in ["90x", "-1d", "d", "9999999d", "$(x)"]:
        with pytest.raises(ValidationError):
            c.valid_expires(bad)


def test_schedule_validator():
    for good in ["off", "daily", "30m", "2h", "*/30 * * * *"]:
        assert c.valid_schedule(good) == good
    for bad in ["99m", "0h", "rm -rf", "* * *", "*/30 * * * * extra", "$(x) * * * *"]:
        with pytest.raises(ValidationError):
            c.valid_schedule(bad)


def test_port_and_bind_and_ip():
    assert c.valid_port("8080") == "8080"
    for bad in ["0", "70000", "abc", "-1"]:
        with pytest.raises(ValidationError):
            c.valid_port(bad)
    assert c.valid_bind("127.0.0.1") == "127.0.0.1"
    with pytest.raises(ValidationError):
        c.valid_bind("0.0.0.0")
    assert c.valid_ip("10.0.0.5") == "10.0.0.5"
    with pytest.raises(ValidationError):
        c.valid_ip("10.0.0.999")


# --------------------------------------------------------------------------- #
# config editor: only whitelisted keys, per-key value validation
def test_env_set_allowlist():
    assert c.build("env.set", key="ENABLE_VECTOR", value="yes") == [SH, "env-set", "ENABLE_VECTOR", "true"]
    assert c.build("env.set", key="HTTP_PORT", value="8080")[2:] == ["HTTP_PORT", "8080"]
    for key in ["SESSION_SECRET", "HTTP_BIND", "DOMAIN", "ALLOW_PLAINTEXT_HTTP", "RANDOM_KEY"]:
        with pytest.raises(ValidationError):
            c.build("env.set", key=key, value="x")
    with pytest.raises(ValidationError):
        c.build("env.set", key="ENABLE_VECTOR", value="maybe")  # not a bool
    with pytest.raises(ValidationError):
        c.build("env.set", key="SEARCH_BACKEND", value="elastic")


def test_wizard_argv():
    assert c.build("wizard", profile="local", port=8080, portal=True) == [
        "/repo/local_deploy.sh", "--yes", "--port", "8080", "--portal",
    ]
    assert c.build("wizard", profile="https", domain="docs.example.com") == [
        "/repo/remote_deploy.sh", "--yes", "--domain", "docs.example.com",
    ]
    assert c.build("wizard", profile="vpn", ip="10.0.0.5", bind="0.0.0.0")[:4] == [
        "/repo/remote_deploy.sh", "--yes", "--ip", "10.0.0.5",
    ]
    # the OpenAI key is NEVER an argv element — vector is enabled by the env var the route
    # sets, so the wizard builder emits no --vector-key at all.
    assert "--vector-key" not in c.build("wizard", profile="local", port=8080)
    with pytest.raises(ValidationError):
        c.build("wizard", profile="https")  # domain required
    with pytest.raises(ValidationError):
        c.build("wizard", profile="bogus")
