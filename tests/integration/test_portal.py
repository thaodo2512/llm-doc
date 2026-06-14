"""Upload/manage portal: staging containment, sessions/CSRF, upload
validation, write-RBAC, and the app end-to-end."""

from __future__ import annotations

import dataclasses
import json

import pytest

from docmcp.atomicio import atomic_write_text
from docmcp.auth import JsonFileTokenVerifier, effective_writable_prefixes
from docmcp.portal import sessions
from docmcp.portal.staging import StagingError, StagingStore
from docmcp.portal.validate import safe_filename, validate_upload


# --------------------------------------------------------------------------- #
# staging containment
def test_staging_resolve_rejects_escapes(tmp_path):
    s = StagingStore(tmp_path / "raw")
    for bad in ["/../etc/passwd", "/a/../../b", "/x\x00y", "/team/../../../etc"]:
        with pytest.raises(StagingError):
            s.resolve(bad)
    # absolute-looking input is contained, not escaping
    assert s.resolve("/etc/passwd") == (s.root / "etc/passwd")


def test_staging_write_list_move_delete(tmp_path):
    s = StagingStore(tmp_path / "raw")
    s.write_atomic("/team-fw/a.md", b"# A\n")
    assert s.is_file("/team-fw/a.md")
    assert s.list_under("/team-fw") == ["/team-fw/a.md"]
    s.move("/team-fw/a.md", "/team-fw/b.md")
    assert s.list_under("/team-fw") == ["/team-fw/b.md"]
    assert s.delete("/team-fw/b.md") and not s.is_file("/team-fw/b.md")


# --------------------------------------------------------------------------- #
# sessions + CSRF
def test_session_sign_verify_roundtrip_and_tamper():
    sess = sessions.new_session("alice", ["/public"], ["/team-fw"])
    cookie = sessions.sign(sess, "secret")
    assert sessions.verify(cookie, "secret") == sess
    assert sessions.verify(cookie, "other-secret") is None  # wrong key
    body, sig = cookie.split(".", 1)
    assert sessions.verify(body + ".AAAA", "secret") is None  # tampered sig


def test_session_expiry():
    sess = sessions.new_session("a", [], ["/x"], ttl=-1)  # already expired
    assert sessions.verify(sessions.sign(sess, "s"), "s") is None


# --------------------------------------------------------------------------- #
# upload validation
def test_validate_upload():
    assert validate_upload("a.md", b"hi", max_bytes=100) is None
    assert "unsupported" in validate_upload("a.exe", b"hi", max_bytes=100)
    assert "too large" in validate_upload("a.md", b"x" * 200, max_bytes=100)
    assert "empty" in validate_upload("a.md", b"", max_bytes=100)
    assert "Git-LFS" in validate_upload("a.pdf", b"version https://git-lfs.github.com/spec/v1", max_bytes=999)
    assert safe_filename("../../etc/passwd") == "etcpasswd" or "passwd" in safe_filename("../../etc/passwd")
    assert safe_filename("/a/b/c.md") == "c.md"


# --------------------------------------------------------------------------- #
# write-RBAC resolution
def test_effective_writable_prefixes():
    assert effective_writable_prefixes({"writable_prefixes": ["/team-fw", "/team-fw"]}) == ["/team-fw"]
    assert effective_writable_prefixes({}) == []  # deny-by-default
    assert effective_writable_prefixes({"writable_prefixes": "/team-fw"}) == []  # not char-split
    assert effective_writable_prefixes({"writable_prefixes": ["/"]}) == ["/"]  # admin --write /
    # whitespace is normalized so " /team " can't masquerade as a distinct prefix
    assert effective_writable_prefixes({"writable_prefixes": [" /team ", "/team", "  "]}) == ["/team"]


async def test_verify_token_surfaces_writable(tmp_path):
    tok = tmp_path / "tokens.json"
    atomic_write_text(
        tok,
        json.dumps({"tok_a": {"user": "a", "allowed_prefixes": ["/public"], "writable_prefixes": ["/team-fw"]}}),
    )
    at = await JsonFileTokenVerifier(tok).verify_token("tok_a")
    assert at.claims["writable_prefixes"] == ["/team-fw"]


# --------------------------------------------------------------------------- #
# the app end-to-end
@pytest.fixture
def portal_client(settings, tmp_path):
    from starlette.testclient import TestClient

    from docmcp.portal.app import build_app

    tok = tmp_path / "ptokens.json"
    atomic_write_text(
        tok,
        json.dumps(
            {
                "tok_w": {"user": "writer", "allowed_prefixes": ["/team-fw"], "writable_prefixes": ["/team-fw"]},
                "tok_r": {"user": "reader", "allowed_prefixes": ["/public"]},  # no writable
            }
        ),
    )
    st = dataclasses.replace(
        settings,
        portal_enabled=True,
        session_secret="testsecret",
        staging_root=tmp_path / "raw",
        tokens_file=tok,
        allow_plaintext_portal=True,  # secure=False so the cookie survives http TestClient
    )
    return TestClient(build_app(st)), tmp_path / "raw"


def _login(client, token):
    r = client.post("/portal/login", data={"token": token}, follow_redirects=False)
    cookie = client.cookies.get("docmcp_portal")
    csrf = sessions.verify(cookie, "testsecret")["csrf"] if cookie else None
    return r, csrf


def test_login_rejects_bad_token(portal_client):
    client, _ = portal_client
    r, _ = _login(client, "nope")
    assert r.status_code == 401


def test_upload_respects_write_rbac_and_csrf(portal_client):
    client, raw = portal_client
    r, csrf = _login(client, "tok_w")
    assert r.status_code == 303 and csrf

    # allowed folder → written
    r = client.post(
        "/portal/upload",
        data={"folder": "/team-fw", "csrf": csrf},
        files={"file": ("note.md", b"# Note\nhi\n", "text/markdown")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert (raw / "team-fw" / "note.md").read_text() == "# Note\nhi\n"

    # outside writable_prefixes → NOT written
    client.post(
        "/portal/upload",
        data={"folder": "/public", "csrf": csrf},
        files={"file": ("x.md", b"x", "text/markdown")},
        follow_redirects=False,
    )
    assert not (raw / "public" / "x.md").exists()

    # bad CSRF → bounced to login, nothing written
    r = client.post(
        "/portal/upload",
        data={"folder": "/team-fw", "csrf": "WRONG"},
        files={"file": ("y.md", b"y\n", "text/markdown")},
        follow_redirects=False,
    )
    assert "/portal/login" in r.headers["location"]
    assert not (raw / "team-fw" / "y.md").exists()


def test_manage_rename_and_delete(portal_client):
    client, raw = portal_client
    _, csrf = _login(client, "tok_w")
    client.post(
        "/portal/upload",
        data={"folder": "/team-fw", "csrf": csrf},
        files={"file": ("a.md", b"a\n", "text/markdown")},
        follow_redirects=False,
    )
    client.post("/portal/rename", data={"src": "/team-fw/a.md", "newname": "b.md", "csrf": csrf}, follow_redirects=False)
    assert (raw / "team-fw" / "b.md").exists() and not (raw / "team-fw" / "a.md").exists()
    # delete requires confirm
    client.post("/portal/delete", data={"src": "/team-fw/b.md", "csrf": csrf}, follow_redirects=False)
    assert (raw / "team-fw" / "b.md").exists()  # not confirmed → still there
    client.post("/portal/delete", data={"src": "/team-fw/b.md", "csrf": csrf, "confirm": "1"}, follow_redirects=False)
    assert not (raw / "team-fw" / "b.md").exists()


def _upload(client, csrf, folder, name, body):
    return client.post(
        "/portal/upload",
        data={"folder": folder, "csrf": csrf},
        files={"file": (name, body, "text/markdown")},
        follow_redirects=False,
    )


def test_rename_rejects_unsupported_extension(portal_client):
    client, raw = portal_client
    _, csrf = _login(client, "tok_w")
    _upload(client, csrf, "/team-fw", "a.md", b"a\n")
    # rename a.md -> a.exe (not in the ingest allowlist) must be refused, file untouched
    r = client.post(
        "/portal/rename",
        data={"src": "/team-fw/a.md", "newname": "a.exe", "csrf": csrf},
        follow_redirects=False,
    )
    assert "unsupported" in r.headers["location"]
    assert (raw / "team-fw" / "a.md").exists() and not (raw / "team-fw" / "a.exe").exists()


def test_rename_refuses_clobber_then_keeps_history(portal_client):
    client, raw = portal_client
    _, csrf = _login(client, "tok_w")
    _upload(client, csrf, "/team-fw", "a.md", b"AAA\n")
    _upload(client, csrf, "/team-fw", "b.md", b"BBB\n")
    # without overwrite → refused; BOTH files keep their content
    r = client.post(
        "/portal/rename",
        data={"src": "/team-fw/a.md", "newname": "b.md", "csrf": csrf},
        follow_redirects=False,
    )
    assert "exists" in r.headers["location"]
    assert (raw / "team-fw" / "a.md").read_text() == "AAA\n"
    assert (raw / "team-fw" / "b.md").read_text() == "BBB\n"
    # with replace=1 → a.md overwrites b.md; b.md's old bytes are kept in history
    client.post(
        "/portal/rename",
        data={"src": "/team-fw/a.md", "newname": "b.md", "csrf": csrf, "replace": "1"},
        follow_redirects=False,
    )
    assert (raw / "team-fw" / "b.md").read_text() == "AAA\n"
    assert not (raw / "team-fw" / "a.md").exists()
    versions = list((raw / ".portal" / "versions").rglob("b.md.*"))
    assert versions and any(v.read_text() == "BBB\n" for v in versions)


def test_move_refuses_clobber_without_overwrite(portal_client):
    client, raw = portal_client
    _, csrf = _login(client, "tok_w")
    # /team-fw/sub is still under the writable prefix, so both writes are allowed
    _upload(client, csrf, "/team-fw", "doc.md", b"TOP\n")
    _upload(client, csrf, "/team-fw/sub", "doc.md", b"SUB\n")
    # move /team-fw/doc.md into /team-fw/sub (already holds doc.md) → refused
    r = client.post(
        "/portal/move",
        data={"src": "/team-fw/doc.md", "folder": "/team-fw/sub", "csrf": csrf},
        follow_redirects=False,
    )
    assert "exists" in r.headers["location"]
    assert (raw / "team-fw" / "doc.md").read_text() == "TOP\n"
    assert (raw / "team-fw" / "sub" / "doc.md").read_text() == "SUB\n"
    # with overwrite → replaces, keeping the clobbered content in history
    client.post(
        "/portal/move",
        data={"src": "/team-fw/doc.md", "folder": "/team-fw/sub", "csrf": csrf, "replace": "1"},
        follow_redirects=False,
    )
    assert (raw / "team-fw" / "sub" / "doc.md").read_text() == "TOP\n"
    assert not (raw / "team-fw" / "doc.md").exists()


def test_read_only_token_has_no_write(portal_client):
    client, raw = portal_client
    _, csrf = _login(client, "tok_r")  # reader: writable_prefixes = []
    client.post(
        "/portal/upload",
        data={"folder": "/public", "csrf": csrf},
        files={"file": ("z.md", b"z\n", "text/markdown")},
        follow_redirects=False,
    )
    assert not (raw / "public" / "z.md").exists()  # deny-by-default


def test_logout_requires_csrf(portal_client):
    client, _ = portal_client
    _, csrf = _login(client, "tok_w")
    client.post("/portal/logout", data={}, follow_redirects=False)  # no csrf → no-op
    assert client.get("/portal", follow_redirects=False).status_code == 200  # still signed in
    client.post("/portal/logout", data={"csrf": csrf}, follow_redirects=False)  # valid → logout
    assert client.get("/portal", follow_redirects=False).status_code == 303  # bounced to login


def test_upload_count_is_capped(settings, tmp_path):
    from starlette.testclient import TestClient

    from docmcp.portal.app import build_app

    tok = tmp_path / "t.json"
    atomic_write_text(tok, json.dumps({"tok_w": {"user": "w", "writable_prefixes": ["/team-fw"]}}))
    st = dataclasses.replace(
        settings,
        portal_enabled=True,
        session_secret="s",
        staging_root=tmp_path / "raw",
        tokens_file=tok,
        allow_plaintext_portal=True,
        max_upload_files=2,
    )
    client = TestClient(build_app(st))
    client.post("/portal/login", data={"token": "tok_w"}, follow_redirects=False)
    csrf = sessions.verify(client.cookies.get("docmcp_portal"), "s")["csrf"]
    files = [("file", (f"f{i}.md", b"x\n", "text/markdown")) for i in range(3)]  # 3 > cap of 2
    client.post("/portal/upload", data={"folder": "/team-fw", "csrf": csrf}, files=files, follow_redirects=False)
    team = tmp_path / "raw" / "team-fw"
    assert not team.exists() or not list(team.glob("*.md"))  # over the cap → nothing written
