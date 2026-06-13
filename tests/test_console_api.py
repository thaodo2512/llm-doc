"""The console JSON API end-to-end (no Docker): auth is required, the admin gate rejects
scoped tokens, CSRF is enforced on mutations, the bootstrap token works only pre-setup,
and secret values never leave the config endpoint. The subprocess layer is replaced with
a fake runner so nothing actually shells out."""

from __future__ import annotations

import json

import httpx

from docmcp.config import Settings
from docmcp.console.app import build_app
from docmcp.console.runner import Job

ADMIN = "tok_admin_aaaaaaaaaaaa"
SCOPED = "tok_bob_bbbbbbbbbbbb"


class FakeRunner:
    """Stand-in for JobRunner — records calls, never spawns a process."""

    def __init__(self):
        self.calls = []
        self._jobs = {}

    def run_sync(self, argv, *, env=None, timeout=120):
        self.calls.append(argv)
        verb = argv[1] if len(argv) > 1 else ""
        if verb in ("token", "token-rotate"):
            return 0, "tok_alice_deadbeefcafe\nexpires in 90d\n"
        return 0, f"[fake] ran {verb}\n"

    def start(self, label, argv, *, env=None, lifecycle=False):
        self.calls.append(argv)
        job = Job(label, argv)
        job.append("[fake] started")
        job.finish(0)
        self._jobs[job.id] = job
        return job

    def get(self, job_id):
        return self._jobs.get(job_id)


def make_app(tmp_path, monkeypatch, *, tokens: dict | None = None, bootstrap: str | None = None, env_text: str | None = None):
    monkeypatch.setenv("DOCMCP_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("TOKENS_FILE", str(tmp_path / "tokens.json"))
    monkeypatch.setenv("DOCSTORE_ROOT", str(tmp_path))
    monkeypatch.setenv("DOC_ROOT", str(tmp_path / "curated"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret-test-secret")
    monkeypatch.setenv("ALLOW_PLAINTEXT_PORTAL", "true")  # loopback http → non-Secure cookie
    if bootstrap is not None:
        monkeypatch.setenv("CONSOLE_BOOTSTRAP_TOKEN", bootstrap)
    else:
        monkeypatch.delenv("CONSOLE_BOOTSTRAP_TOKEN", raising=False)
    if tokens is not None:
        (tmp_path / "tokens.json").write_text(json.dumps(tokens), encoding="utf-8")
    if env_text is not None:
        (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    settings = Settings.load(dotenv=False)
    app = build_app(settings)
    app.state.console.runner = FakeRunner()
    return app


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _login(ac, token):
    r = await ac.post("/api/login", json={"token": token})
    return r


# --------------------------------------------------------------------------- #
async def test_admin_login_and_gate(tmp_path, monkeypatch):
    app = make_app(
        tmp_path,
        monkeypatch,
        tokens={
            ADMIN: {"user": "admin", "allowed_prefixes": ["/"]},
            SCOPED: {"user": "bob", "allowed_prefixes": ["/public"]},
        },
    )
    async with client(app) as ac:
        # scoped token is rejected by the admin gate
        r = await _login(ac, SCOPED)
        assert r.status_code == 403
        # bad token → 401
        assert (await _login(ac, "nope")).status_code == 401
        # admin token works
        r = await _login(ac, ADMIN)
        assert r.status_code == 200 and r.json()["role"] == "admin"
        assert r.json()["csrf"]


async def test_reads_require_auth(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, tokens={ADMIN: {"user": "admin", "allowed_prefixes": ["/"]}})
    async with client(app) as ac:
        assert (await ac.get("/api/tokens")).status_code == 401
        await _login(ac, ADMIN)
        assert (await ac.get("/api/tokens")).status_code == 200


async def test_csrf_enforced_on_mutation(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, tokens={ADMIN: {"user": "admin", "allowed_prefixes": ["/"]}})
    async with client(app) as ac:
        csrf = (await _login(ac, ADMIN)).json()["csrf"]
        body = {"user": "alice", "prefixes": ["/public"]}
        # no CSRF header → 403
        assert (await ac.post("/api/tokens", json=body)).status_code == 403
        # wrong CSRF → 403
        assert (await ac.post("/api/tokens", json=body, headers={"X-CSRF-Token": "x"})).status_code == 403
        # correct CSRF → 200 and a token comes back once
        r = await ac.post("/api/tokens", json=body, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200 and r.json()["token"].startswith("tok_")


async def test_mint_validation_error_is_400(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, tokens={ADMIN: {"user": "admin", "allowed_prefixes": ["/"]}})
    async with client(app) as ac:
        csrf = (await _login(ac, ADMIN)).json()["csrf"]
        r = await ac.post("/api/tokens", json={"user": "--all", "prefixes": ["/x"]}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 400 and "error" in r.json()


async def test_lifecycle_returns_job(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, tokens={ADMIN: {"user": "admin", "allowed_prefixes": ["/"]}})
    async with client(app) as ac:
        csrf = (await _login(ac, ADMIN)).json()["csrf"]
        r = await ac.post("/api/lifecycle/ingest", json={"full": True}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        s = await ac.get(f"/api/jobs/{job_id}")
        assert s.status_code == 200 and s.json()["status"] == "done"
        log = await ac.get(f"/api/jobs/{job_id}/log")
        assert "[fake] started" in log.json()["lines"]


async def test_config_redacts_secrets(tmp_path, monkeypatch):
    app = make_app(
        tmp_path,
        monkeypatch,
        tokens={ADMIN: {"user": "admin", "allowed_prefixes": ["/"]}},
        env_text="OPENAI_API_KEY=sk-supersecret\nSESSION_SECRET=hunter2\nHTTP_PORT=8080\n",
    )
    async with client(app) as ac:
        await _login(ac, ADMIN)
        data = (await ac.get("/api/config")).json()
        blob = json.dumps(data)
        assert "sk-supersecret" not in blob and "hunter2" not in blob
        rows = {row["key"]: row for row in data["env"]}
        assert rows["OPENAI_API_KEY"]["value"] == "***set***" and rows["OPENAI_API_KEY"]["secret"]
        assert rows["HTTP_PORT"]["value"] == "8080" and rows["HTTP_PORT"]["editable"]


# --------------------------------------------------------------------------- #
# bootstrap (pre-setup) flow
async def test_bootstrap_only_before_setup(tmp_path, monkeypatch):
    # no tokens.json yet → bootstrap mode
    app = make_app(tmp_path, monkeypatch, bootstrap="boot-secret-123")
    async with client(app) as ac:
        sess = (await ac.get("/api/session")).json()
        assert sess["bootstrap_active"] and not sess["setup_done"]
        # wrong bootstrap token → 401
        assert (await ac.post("/api/login", json={"bootstrap": "wrong"})).status_code == 401
        # right bootstrap token → a bootstrap session
        r = await ac.post("/api/login", json={"bootstrap": "boot-secret-123"})
        assert r.status_code == 200 and r.json()["role"] == "bootstrap"
        # a bootstrap session can hit the wizard guard (admin=False) but NOT admin reads
        assert (await ac.get("/api/tokens")).status_code == 401  # admin-only read
        # now setup "completes": an admin token appears
        (tmp_path / "tokens.json").write_text(json.dumps({ADMIN: {"user": "admin", "allowed_prefixes": ["/"]}}))
        # the live bootstrap session is now invalid
        assert (await ac.get("/api/session")).json()["authenticated"] is False
        # and bootstrap login is refused
        assert (await ac.post("/api/login", json={"bootstrap": "boot-secret-123"})).status_code == 401


async def test_bootstrap_can_drive_wizard(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, bootstrap="boot-xyz")
    async with client(app) as ac:
        csrf = (await ac.post("/api/login", json={"bootstrap": "boot-xyz"})).json()["csrf"]
        r = await ac.post(
            "/api/wizard/apply", json={"profile": "local", "port": 8080}, headers={"X-CSRF-Token": csrf}
        )
        assert r.status_code == 202 and r.json()["job_id"]
