"""Static guards that lock the console's security invariants in place — things that are
load-bearing but awkward to exercise live: the subprocess perimeter (no shells, argv only
built in commands.py / executed in runner.py), the docmcp.sh loopback + bootstrap guards,
and the admin gate."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "src" / "docmcp" / "console"
SH = (ROOT / "docmcp.sh").read_text(encoding="utf-8")


def _sh_body(name: str) -> str:
    start = re.search(rf"\n{re.escape(name)}\(\)\s*\{{", SH)
    assert start, f"{name} not found in docmcp.sh"
    rest = SH[start.end():]
    nxt = re.search(r"\n(?:cmd_[a-z_]+|_cron_[a-z]+|usage)\(\)\s*\{", rest)
    return rest[: nxt.start()] if nxt else rest


def _src(name: str) -> str:
    return (CONSOLE / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# subprocess perimeter
def test_no_shell_true_or_os_system_anywhere():
    for path in CONSOLE.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "shell=True" not in text, f"shell=True in {path.name}"
        assert "os.system" not in text, f"os.system in {path.name}"


def test_only_runner_spawns_processes():
    # subprocess is imported/used ONLY in runner.py; everything else goes through it.
    # (Check code tokens — imports/calls — not prose, so a docstring mention is fine.)
    for path in CONSOLE.glob("*.py"):
        if path.name == "runner.py":
            continue
        text = path.read_text(encoding="utf-8")
        for tok in ("import subprocess", "subprocess.", "Popen("):
            assert tok not in text, f"{tok!r} used outside runner.py: {path.name}"


def test_commands_module_does_no_io():
    # The allowlist must be pure: it builds argv, it does not run or open anything.
    text = _src("commands.py")
    for tok in ("import subprocess", "subprocess.", "Popen(", "open("):
        assert tok not in text, f"commands.py must do no I/O, found {tok!r}"


# --------------------------------------------------------------------------- #
# auth: admin gate + bootstrap
def test_auth_enforces_admin_gate():
    text = _src("auth.py")
    assert "is_admin_claims" in text  # whole-corpus check on login
    assert "not-admin" in text  # scoped token → 403 sentinel
    # a bootstrap session dies once setup has minted the admin token
    assert "setup_done()" in text and 'role") == "bootstrap"' in text


def test_routes_admin_default_and_csrf():
    text = _src("routes.py")
    # mutations build through commands.build (never a raw argv) and check CSRF
    assert "self._guard(request)" in text
    assert "CSRF_HEADER" in text and "x-csrf-token" in text


# --------------------------------------------------------------------------- #
# docmcp.sh cmd_console: loopback-only + bootstrap + DooD wiring
def test_cmd_console_refuses_non_loopback():
    body = _sh_body("cmd_console")
    assert "127.0.0.1|localhost|::1" in body  # loopback allowlist
    assert "must stay on loopback" in body  # the hard refusal
    assert "ssh -L" in body  # the documented remote path


def test_cmd_console_bootstrap_and_dood():
    body = _sh_body("cmd_console")
    assert "CONSOLE_BOOTSTRAP_TOKEN" in body  # in-memory bootstrap token passed in
    assert "/var/run/docker.sock" in body  # docker-out-of-docker socket
    assert '-v "$ROOT:$ROOT" -w "$ROOT"' in body  # same-path repo mount (path identity)
    assert "docs-mcp:console" in body


def test_menu_is_default_and_routes():
    # Running docmcp.sh with no args opens the interactive menu (help on a non-TTY),
    # and the menu routes to the console + both deploy wizards.
    assert 'cmd="${1:-menu}"' in SH
    assert "menu|start)" in SH
    body = _sh_body("cmd_menu")
    assert "[ ! -t 0 ]" in body  # non-interactive → fall back to the full help, never hang
    assert "cmd_console" in body
    assert "local_deploy.sh" in body and "remote_deploy.sh" in body


def test_env_set_is_atomic_and_wired():
    body = _sh_body("cmd_env_set")
    assert "mktemp" in body and "mv " in body  # atomic temp + replace, not in-place
    assert "sed -i" not in body
    assert "\n  env-set)" in SH or "env-set)" in SH  # dispatch entry exists
    assert "console)" in SH  # console dispatch entry exists
