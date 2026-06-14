"""HTTP handlers for the console JSON API.

Thin handlers: parse → validate via ``commands.build`` → run (sync read, or a job) →
JSON. Mutations require an admin session + a matching ``X-CSRF-Token`` header. Long ops
(build/ingest/serve/stop/backup/wizard) return ``202 {job_id}`` and stream over SSE.
"""

from __future__ import annotations

import asyncio
import json
import os

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

from ..config import Settings
from . import commands, reads
from .audit import ConsoleAudit, redact_argv
from .auth import ConsoleAuth
from .commands import ValidationError
from .runner import JobBusy, JobRunner

CSRF_HEADER = "x-csrf-token"


def _json(data, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


class Console:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.auth = ConsoleAuth(settings)
        self.root = commands.repo_root()
        self.runner = JobRunner(cwd=str(self.root))
        self.audit = ConsoleAudit(self.root)

    # -- helpers -------------------------------------------------------------
    async def _body(self, request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _guard(self, request: Request, *, admin: bool = True) -> dict | None:
        return self.auth.guard(request, request.headers.get(CSRF_HEADER), admin=admin)

    def _verb(self, action: str, **kwargs) -> Response:
        """Run a read-only verb synchronously and return its text output."""
        try:
            argv = commands.build(action, **kwargs)
        except ValidationError as exc:
            return _json({"error": str(exc)}, 400)
        rc, out = self.runner.run_sync(argv)
        return _json({"ok": rc == 0, "exit_code": rc, "output": out})

    def _run_mutation(self, sess: dict, action: str, *, env: dict | None = None, **kwargs):
        """Build + run a quick mutating verb synchronously; audit the outcome.
        Returns ``(exit_code, output)`` or raises :class:`ValidationError`."""
        argv = commands.build(action, **kwargs)
        rc, out = self.runner.run_sync(argv, env=env)
        self.audit.record(
            user=sess.get("user", "?"),
            action=action,
            result="ok" if rc == 0 else "error",
            argv=redact_argv(argv),
        )
        return rc, out

    # -- health / auth -------------------------------------------------------
    async def healthz(self, request: Request) -> Response:
        return PlainTextResponse("ok")

    async def login(self, request: Request) -> Response:
        body = await self._body(request)
        result = await self.auth.authenticate(
            token=(body.get("token") or "").strip(), bootstrap=(body.get("bootstrap") or "").strip()
        )
        if result == "not-admin":
            return _json({"error": "this console requires the admin (whole-corpus) token"}, 403)
        if not result:
            return _json({"error": "invalid or expired token"}, 401)
        resp = _json({"ok": True, "user": result["user"], "role": result["role"], "csrf": result["csrf"]})
        self.auth.set_cookie(resp, result)
        return resp

    async def logout(self, request: Request) -> Response:
        if not self._guard(request, admin=False):
            return _json({"error": "forbidden"}, 403)
        resp = _json({"ok": True})
        self.auth.clear_cookie(resp)
        return resp

    async def session(self, request: Request) -> Response:
        sess = self.auth.session(request)
        out = {
            "authenticated": bool(sess),
            "setup_done": self.auth.setup_done(),
            "bootstrap_active": self.auth.bootstrap_active(),
        }
        if sess:
            out.update(user=sess.get("user"), role=sess.get("role"), csrf=sess.get("csrf"))
        # The folder cmd_console was pointed at (if any) — so the wizard can tell the user it will
        # be imported + indexed during setup.
        out["import_dir"] = (os.environ.get("CONSOLE_IMPORT_NAME") or "").strip() or None
        return _json(out)

    # -- reads (Docker-backed text) ------------------------------------------
    async def status(self, request: Request) -> Response:
        if not self.auth.require(request, admin=False):
            return _json({"error": "unauthorized"}, 401)
        return self._verb("status")

    async def doctor(self, request: Request) -> Response:
        if not self.auth.require(request):
            return _json({"error": "unauthorized"}, 401)
        return self._verb("doctor")

    async def inventory(self, request: Request) -> Response:
        if not self.auth.require(request):
            return _json({"error": "unauthorized"}, 401)
        return self._verb("inventory")

    # -- reads (direct, structured) ------------------------------------------
    async def config(self, request: Request) -> Response:
        if not self.auth.require(request, admin=False):
            return _json({"error": "unauthorized"}, 401)
        return _json(reads.config_view(self.settings))

    async def tokens(self, request: Request) -> Response:
        if not self.auth.require(request):
            return _json({"error": "unauthorized"}, 401)
        return _json({"tokens": reads.list_tokens(self.settings)})

    async def groups(self, request: Request) -> Response:
        if not self.auth.require(request):
            return _json({"error": "unauthorized"}, 401)
        return _json({"groups": reads.list_groups(self.settings)})

    async def access_check(self, request: Request) -> Response:
        if not self.auth.require(request):
            return _json({"error": "unauthorized"}, 401)
        user = request.query_params.get("user", "")
        path = request.query_params.get("path", "")
        if not user or not path:
            return _json({"error": "user and path are required"}, 400)
        return _json(reads.access_check(self.settings, user, path))

    async def access_tree(self, request: Request) -> Response:
        if not self.auth.require(request):
            return _json({"error": "unauthorized"}, 401)
        return _json(reads.access_tree(self.settings))

    async def audit(self, request: Request) -> Response:
        if not self.auth.require(request):
            return _json({"error": "unauthorized"}, 401)
        try:
            n = int(request.query_params.get("n", "50"))
        except ValueError:
            n = 50
        return _json(
            {
                "tokens": reads.audit_tail(self.root / "var" / "token-audit.jsonl", n),
                "console": reads.audit_tail(self.audit.path, n),
            }
        )

    # -- token mutations -----------------------------------------------------
    async def token_mint(self, request: Request) -> Response:
        sess = self._guard(request)
        if not sess:
            return _json({"error": "forbidden"}, 403)
        body = await self._body(request)
        try:
            rc, out = self._run_mutation(
                sess,
                "token.mint",
                user=body.get("user", ""),
                prefixes=body.get("prefixes"),
                groups=body.get("groups"),
                writes=body.get("writes"),
                expires=body.get("expires"),
                comment=body.get("comment"),
                admin=bool(body.get("admin")),
            )
        except ValidationError as exc:
            return _json({"error": str(exc)}, 400)
        token = next((ln.strip() for ln in out.splitlines() if ln.strip().startswith("tok_")), None)
        if rc != 0 or not token:
            return _json({"ok": False, "output": out}, 400 if rc != 0 else 500)
        return _json({"ok": True, "token": token, "output": out})

    async def token_revoke(self, request: Request) -> Response:
        sess = self._guard(request)
        if not sess:
            return _json({"error": "forbidden"}, 403)
        body = await self._body(request)
        try:
            rc, out = self._run_mutation(sess, "token.revoke", ref=body.get("ref", ""))
        except ValidationError as exc:
            return _json({"error": str(exc)}, 400)
        return _json({"ok": rc == 0, "output": out}, 200 if rc == 0 else 400)

    async def token_rotate(self, request: Request) -> Response:
        sess = self._guard(request)
        if not sess:
            return _json({"error": "forbidden"}, 403)
        body = await self._body(request)
        try:
            rc, out = self._run_mutation(sess, "token.rotate", user=body.get("user", ""))
        except ValidationError as exc:
            return _json({"error": str(exc)}, 400)
        token = next((ln.strip() for ln in out.splitlines() if ln.strip().startswith("tok_")), None)
        return _json({"ok": rc == 0, "token": token, "output": out}, 200 if rc == 0 else 400)

    # -- group mutations -----------------------------------------------------
    async def group_define(self, request: Request) -> Response:
        sess = self._guard(request)
        if not sess:
            return _json({"error": "forbidden"}, 403)
        body = await self._body(request)
        try:
            rc, out = self._run_mutation(
                sess, "group.define", name=body.get("name", ""), prefixes=body.get("prefixes")
            )
        except ValidationError as exc:
            return _json({"error": str(exc)}, 400)
        return _json({"ok": rc == 0, "output": out}, 200 if rc == 0 else 400)

    async def group_remove(self, request: Request) -> Response:
        sess = self._guard(request)
        if not sess:
            return _json({"error": "forbidden"}, 403)
        body = await self._body(request)
        try:
            rc, out = self._run_mutation(sess, "group.remove", name=body.get("name", ""))
        except ValidationError as exc:
            return _json({"error": str(exc)}, 400)
        return _json({"ok": rc == 0, "output": out}, 200 if rc == 0 else 400)

    # -- config edit + schedule ----------------------------------------------
    async def config_set(self, request: Request) -> Response:
        sess = self._guard(request)
        if not sess:
            return _json({"error": "forbidden"}, 403)
        body = await self._body(request)
        try:
            rc, out = self._run_mutation(
                sess, "env.set", key=body.get("key", ""), value=body.get("value", "")
            )
        except ValidationError as exc:
            return _json({"error": str(exc)}, 400)
        return _json(
            {"ok": rc == 0, "output": out, "note": "restart the server (Serve) for this to take effect"},
            200 if rc == 0 else 400,
        )

    async def schedule_show(self, request: Request) -> Response:
        if not self.auth.require(request):
            return _json({"error": "unauthorized"}, 401)
        return self._verb("schedule.show")

    async def schedule_set(self, request: Request) -> Response:
        sess = self._guard(request)
        if not sess:
            return _json({"error": "forbidden"}, 403)
        body = await self._body(request)
        try:
            rc, out = self._run_mutation(sess, "schedule.set", spec=body.get("spec", ""))
        except ValidationError as exc:
            return _json({"error": str(exc)}, 400)
        return _json({"ok": rc == 0, "output": out}, 200 if rc == 0 else 400)

    # -- lifecycle jobs ------------------------------------------------------
    def _start_job(self, sess: dict, label: str, action: str, *, env: dict | None = None, **kwargs):
        argv = commands.build(action, **kwargs)
        job = self.runner.start(label, argv, env=env, lifecycle=True)
        self.audit.record(
            user=sess.get("user", "?"), action=action, result="started", job_id=job.id, argv=redact_argv(argv)
        )
        return job

    async def _lifecycle(self, request: Request, label: str, action: str, **kwargs) -> Response:
        sess = self._guard(request)
        if not sess:
            return _json({"error": "forbidden"}, 403)
        try:
            job = self._start_job(sess, label, action, **kwargs)
        except ValidationError as exc:
            return _json({"error": str(exc)}, 400)
        except JobBusy as exc:
            return _json({"error": str(exc)}, 409)
        return _json({"job_id": job.id, "label": label}, 202)

    async def build(self, request: Request) -> Response:
        body = await self._body(request)
        return await self._lifecycle(request, "build", "build", target=body.get("target", "server"))

    async def ingest(self, request: Request) -> Response:
        body = await self._body(request)
        return await self._lifecycle(request, "ingest", "ingest", full=bool(body.get("full")))

    async def serve(self, request: Request) -> Response:
        return await self._lifecycle(request, "serve", "serve")

    async def stop(self, request: Request) -> Response:
        return await self._lifecycle(request, "stop", "stop")

    async def backup(self, request: Request) -> Response:
        return await self._lifecycle(request, "backup", "backup")

    async def wizard_apply(self, request: Request) -> Response:
        # The wizard runs during first-run too, so a bootstrap session may drive it.
        sess = self._guard(request, admin=False)
        if not sess:
            return _json({"error": "forbidden"}, 403)
        body = await self._body(request)
        env = None
        vector_local = False
        if body.get("vector"):
            raw_key = (body.get("vector_key") or "").strip()
            if raw_key:
                # Legacy OpenAI backend: pass the key via env (never argv).
                try:
                    key = commands.clean_text(raw_key, field="vector_key", maxlen=512)
                except ValidationError as exc:
                    return _json({"error": str(exc)}, 400)
                env = dict(os.environ, DOCMCP_OPENAI_API_KEY=key)
            else:
                # No key → the OFFLINE local embedder (the default, air-gap-safe).
                vector_local = True
        # Import the folder the operator pointed cmd_console at (CONSOLE_IMPORT_DIR) as part of the
        # deploy, so first-run setup actually indexes a corpus. The path comes from trusted server
        # env (set at launch), never from the browser; the client only opts out via import=false.
        docs = None
        if body.get("import", True):
            docs = (os.environ.get("CONSOLE_IMPORT_DIR") or "").strip() or None
        try:
            job = self._start_job(
                sess,
                "setup wizard",
                "wizard",
                env=env,
                profile=body.get("profile", ""),
                port=body.get("port"),
                bind=body.get("bind"),
                ip=body.get("ip"),
                domain=body.get("domain"),
                portal=bool(body.get("portal")),
                vector_local=vector_local,
                schedule=body.get("schedule"),
                docs=docs,
            )
        except ValidationError as exc:
            return _json({"error": str(exc)}, 400)
        except JobBusy as exc:
            return _json({"error": str(exc)}, 409)
        return _json({"job_id": job.id, "label": "setup wizard"}, 202)

    async def wizard_token(self, request: Request) -> Response:
        # The just-minted admin token, so the wizard's completion screen can SHOW it (with a copy
        # button) instead of telling the user to scroll the log. The first-run wizard mints the
        # admin token mid-deploy, which flips setup_done and turns its bootstrap session stale — so
        # allow_stale_bootstrap lets that very session read back the token it just created. This is
        # the same person who launched the loopback-only console with the one-time bootstrap secret,
        # and admins can already retrieve this token via /api/connect, so it widens nothing. Returns
        # {has_token: false} before setup (no token exists yet).
        if not self.auth.require(request, admin=False, allow_stale_bootstrap=True):
            return _json({"error": "unauthorized"}, 401)
        token = reads.client_bearer_token(self.settings)
        return _json({"token": token, "has_token": bool(token)})

    # -- job inspection ------------------------------------------------------
    async def job_status(self, request: Request) -> Response:
        # allow_stale_bootstrap: the first-run wizard runs under a bootstrap session that setup
        # invalidates mid-deploy; it must still be able to watch its job finish.
        if not self.auth.require(request, admin=False, allow_stale_bootstrap=True):
            return _json({"error": "unauthorized"}, 401)
        job = self.runner.get(request.path_params["job_id"])
        if not job:
            return _json({"error": "no such job"}, 404)
        return _json(job.to_dict())

    async def job_log(self, request: Request) -> Response:
        if not self.auth.require(request, admin=False, allow_stale_bootstrap=True):
            return _json({"error": "unauthorized"}, 401)
        job = self.runner.get(request.path_params["job_id"])
        if not job:
            return _json({"error": "no such job"}, 404)
        try:
            after = int(request.query_params.get("after", "0"))
        except ValueError:
            after = 0
        cursor, lines = job.tail(after)
        return _json({"cursor": cursor, "lines": lines, "status": job.status, "exit_code": job.exit_code})

    async def job_stream(self, request: Request) -> Response:
        if not self.auth.require(request, admin=False, allow_stale_bootstrap=True):
            return _json({"error": "unauthorized"}, 401)
        job = self.runner.get(request.path_params["job_id"])
        if not job:
            return _json({"error": "no such job"}, 404)

        async def gen():
            cursor = 0
            idle = 0
            while True:
                cursor, lines = job.tail(cursor)
                for line in lines:
                    yield f"event: line\ndata: {json.dumps(line)}\n\n"
                    idle = 0
                if lines:
                    continue
                if job.done:
                    yield f"event: done\ndata: {json.dumps({'exit_code': job.exit_code, 'status': job.status})}\n\n"
                    return
                idle += 1
                if idle % 6 == 0:  # ~ every 1.5s of quiet, keep the connection warm
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    # -- connection helper ---------------------------------------------------
    async def connect(self, request: Request) -> Response:
        if not self.auth.require(request, admin=False):
            return _json({"error": "unauthorized"}, 401)
        # URL from the DEPLOYED .env (right port/domain), not the console's stale launch env.
        url = reads.public_mcp_url(self.settings)
        # Embed the whole-corpus token minted at setup so the command is ready to run — no minting
        # or looking it up. The doc MCP server is read-only, so this grants reads of the corpus;
        # the token's write scope only matters to the upload portal. None only before setup.
        token = reads.client_bearer_token(self.settings)
        bearer = token or "<paste your tok_… token>"
        # The maintainer-endorsed Codex wiring (same as the deploy prints): register over HTTP with
        # the token via env var — `codex mcp add` writes the config, so there's nothing to hand-edit.
        codex_cmd = (
            f"export DOCS_MCP_TOKEN={bearer}\n"
            f"codex mcp add docs --url {url} --bearer-token-env-var DOCS_MCP_TOKEN"
        )
        return _json({"url": url, "codex_cmd": codex_cmd, "has_token": bool(token)})
