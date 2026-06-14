"""The portal Starlette app + ``docmcp-portal`` uvicorn entrypoint.

Login verifies a pasted bearer token, issues a signed session cookie, and exposes
upload + manage (rename/move/delete) confined to the caller's ``writable_prefixes``.
It writes ONLY to the staging (``raw/``) area and never runs Docker — the existing cron
``schedule`` ingests what it writes. ``docs-mcp`` stays read-only.
"""

from __future__ import annotations

import json
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import rbac
from ..auth import JsonFileTokenVerifier
from ..config import Settings
from . import render, sessions
from .staging import StagingError, StagingStore
from .validate import has_allowed_ext, safe_filename, validate_upload

COOKIE = "docmcp_portal"


class Portal:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.verifier = JsonFileTokenVerifier(settings.tokens_file, settings.groups_file)
        self.staging = StagingStore(settings.staging_dir)
        self.secure = not settings.allow_plaintext_portal  # Secure cookie unless plaintext opt-in
        self.insecure = settings.allow_plaintext_portal
        self._state = self.staging.root / ".portal"  # under raw/ → a dotdir ingest skips
        self.audit_path = self._state / "audit.jsonl"
        self.versions_dir = self._state / "versions"

    # -- session helpers ------------------------------------------------------
    def session(self, request: Request) -> dict | None:
        raw = request.cookies.get(COOKIE)
        return sessions.verify(raw, self.settings.session_secret) if raw else None

    def set_cookie(self, resp: Response, session: dict) -> None:
        resp.set_cookie(
            COOKIE,
            sessions.sign(session, self.settings.session_secret),
            httponly=True,
            samesite="strict",
            secure=self.secure,
            max_age=sessions.SESSION_TTL,
            path="/portal",
        )

    def audit(self, **fields) -> None:
        fields["ts"] = int(time.time())
        try:
            self._state.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(fields, sort_keys=True) + "\n")
        except OSError:
            pass

    def can_write(self, session: dict, logical: str) -> str | None:
        """Resolve-then-check: canonicalize the target under STAGING_ROOT, then RBAC.
        Returns the canonical logical path if allowed, else None."""
        try:
            canonical = self.staging.to_logical(self.staging.resolve(logical))
        except StagingError:
            return None
        if not rbac.is_allowed(canonical, session.get("writable_prefixes") or []):
            return None
        return canonical

    # -- pages ----------------------------------------------------------------
    async def login_get(self, request: Request) -> Response:
        if self.session(request):
            return RedirectResponse("/portal", status_code=303)
        return HTMLResponse(render.login_page(insecure=self.insecure))

    async def login_post(self, request: Request) -> Response:
        form = await request.form()
        token = (form.get("token") or "").strip()
        access = await self.verifier.verify_token(token) if token else None
        if access is None:
            return HTMLResponse(
                render.login_page(error="Invalid or expired token.", insecure=self.insecure),
                status_code=401,
            )
        sess = sessions.new_session(
            access.claims["user"],
            access.claims.get("allowed_prefixes", []),
            access.claims.get("writable_prefixes", []),
        )
        resp = RedirectResponse("/portal", status_code=303)
        self.set_cookie(resp, sess)
        return resp

    async def logout_post(self, request: Request) -> Response:
        # CSRF-checked like the other mutations; a bad/absent token just no-ops.
        form = await request.form()
        if not self._guard(request, form):
            return RedirectResponse("/portal/login", status_code=303)
        resp = RedirectResponse("/portal/login", status_code=303)
        resp.delete_cookie(
            COOKIE, path="/portal", samesite="strict", secure=self.secure, httponly=True
        )
        return resp

    async def dashboard(self, request: Request) -> Response:
        sess = self.session(request)
        if not sess:
            return RedirectResponse("/portal/login", status_code=303)
        writable = sess.get("writable_prefixes") or []
        files: list[str] = []
        for prefix in writable:
            files.extend(self.staging.list_under(prefix))
        files = sorted(dict.fromkeys(files))
        return HTMLResponse(
            render.dashboard(
                user=sess["user"],
                writable=writable,
                files=files,
                csrf=sess["csrf"],
                message=request.query_params.get("m"),
                error=request.query_params.get("e"),
                insecure=self.insecure,
            )
        )

    # -- mutations (CSRF-checked) --------------------------------------------
    def _guard(self, request: Request, form) -> dict | None:
        sess = self.session(request)
        if not sess:
            return None
        if (form.get("csrf") or "") != sess.get("csrf"):
            return None
        return sess

    async def upload(self, request: Request) -> Response:
        # Coarse body-size preflight before parsing (bounds memory/disk).
        cap = self.settings.max_upload_bytes * self.settings.max_upload_files + 1_048_576
        clen = request.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > cap:
            return RedirectResponse("/portal?e=upload+too+large", status_code=303)
        # Bound the multipart parser itself (caps the count even on a chunked request
        # with no Content-Length, which would otherwise skip the preflight above).
        try:
            form = await request.form(
                max_files=self.settings.max_upload_files,
                max_fields=self.settings.max_upload_files + 20,
            )
        except Exception:
            return RedirectResponse("/portal?e=too+many+files+or+bad+request", status_code=303)
        sess = self._guard(request, form)
        if not sess:
            return RedirectResponse("/portal/login", status_code=303)
        folder = (form.get("folder") or "").strip()
        uploads = [f for f in form.getlist("file") if hasattr(f, "read")]
        if not uploads:
            return RedirectResponse("/portal?e=no+file", status_code=303)
        if len(uploads) > self.settings.max_upload_files:
            return RedirectResponse("/portal?e=too+many+files", status_code=303)
        ok, errs, total = 0, [], 0
        for up in uploads:
            name = safe_filename(getattr(up, "filename", "") or "")
            data = await up.read(self.settings.max_upload_bytes + 1)
            total += len(data)
            if total > cap:  # bound aggregate bytes even when Content-Length was absent
                errs.append("aborted: request exceeded the total upload limit")
                break
            err = validate_upload(name, data, max_bytes=self.settings.max_upload_bytes)
            if err:
                errs.append(f"{name or '?'}: {err}")
                continue
            logical = f"{folder.rstrip('/')}/{name}"
            canonical = self.can_write(sess, logical)
            if canonical is None:
                errs.append(f"{name}: not allowed in {folder}")
                continue
            self._keep_history(canonical)
            self.staging.write_atomic(canonical, data)
            self.audit(
                user=sess["user"], action="upload", path=canonical, bytes=len(data), result="ok"
            )
            ok += 1
        for e in errs:
            self.audit(user=sess["user"], action="upload", result="rejected", reason=e)
        msg = f"uploaded {ok} file(s)" + (f"; {len(errs)} rejected" if errs else "")
        q = "m=" + msg.replace(" ", "+") if not errs else "e=" + "; ".join(errs).replace(" ", "+")
        return RedirectResponse(f"/portal?{q}", status_code=303)

    def _keep_history(self, canonical: str, keep: int = 10) -> None:
        """Before overwriting an existing file, copy it to .portal/versions/<rel>.<ts>,
        pruning to the most recent ``keep`` versions per path (bounds disk growth)."""
        try:
            current = self.staging.resolve(canonical)
            if not current.is_file():
                return
            rel = canonical.lstrip("/")
            dest = self.versions_dir / f"{rel}.{int(time.time())}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(current.read_bytes())
            prior = sorted(dest.parent.glob(dest.name.rsplit(".", 1)[0] + ".*"))
            for stale in prior[:-keep]:  # keep only the newest `keep`
                stale.unlink(missing_ok=True)
        except (OSError, StagingError):
            pass

    async def rename(self, request: Request) -> Response:
        form = await request.form()
        sess = self._guard(request, form)
        if not sess:
            return RedirectResponse("/portal/login", status_code=303)
        src = self.can_write(sess, form.get("src") or "")
        newname = safe_filename(form.get("newname") or "")
        if not src or not newname:
            return RedirectResponse("/portal?e=invalid+rename", status_code=303)
        if not has_allowed_ext(newname):  # a rename must not smuggle in an un-ingestable type
            return RedirectResponse("/portal?e=unsupported+target+type", status_code=303)
        dst_logical = src.rsplit("/", 1)[0] + "/" + newname
        dst = self.can_write(sess, dst_logical)
        if not dst:
            return RedirectResponse("/portal?e=rename+not+allowed", status_code=303)
        if dst != src and self.staging.is_file(dst):  # don't silently clobber a different file
            if not form.get("replace"):
                return RedirectResponse("/portal?e=destination+exists+(tick+overwrite)", status_code=303)
            self._keep_history(dst)  # preserve what we are about to overwrite
        try:
            self.staging.move(src, dst)
        except (StagingError, OSError):  # OSError e.g. dst is an existing dir → clean 303, not 500
            return RedirectResponse("/portal?e=rename+failed", status_code=303)
        self.audit(user=sess["user"], action="rename", path=src, to=dst, result="ok")
        return RedirectResponse("/portal?m=renamed", status_code=303)

    async def move(self, request: Request) -> Response:
        form = await request.form()
        sess = self._guard(request, form)
        if not sess:
            return RedirectResponse("/portal/login", status_code=303)
        src = self.can_write(sess, form.get("src") or "")
        folder = (form.get("folder") or "").strip()
        if not src:
            return RedirectResponse("/portal?e=invalid+move", status_code=303)
        dst_logical = f"{folder.rstrip('/')}/{src.rsplit('/', 1)[1]}"
        dst = self.can_write(sess, dst_logical)
        if not dst:
            return RedirectResponse("/portal?e=move+not+allowed", status_code=303)
        if dst == src:
            return RedirectResponse("/portal?m=moved", status_code=303)  # same folder → no-op
        if self.staging.is_file(dst):  # don't silently clobber a file already in the target
            if not form.get("replace"):
                return RedirectResponse("/portal?e=destination+exists+(tick+overwrite)", status_code=303)
            self._keep_history(dst)
        try:
            self.staging.move(src, dst)
        except (StagingError, OSError):  # OSError e.g. dst is an existing dir → clean 303, not 500
            return RedirectResponse("/portal?e=move+failed", status_code=303)
        self.audit(user=sess["user"], action="move", path=src, to=dst, result="ok")
        return RedirectResponse("/portal?m=moved", status_code=303)

    async def delete(self, request: Request) -> Response:
        form = await request.form()
        sess = self._guard(request, form)
        if not sess:
            return RedirectResponse("/portal/login", status_code=303)
        if not form.get("confirm"):
            return RedirectResponse("/portal?e=delete+not+confirmed", status_code=303)
        src = self.can_write(sess, form.get("src") or "")
        if not src or not self.staging.delete(src):
            return RedirectResponse("/portal?e=delete+failed", status_code=303)
        self.audit(user=sess["user"], action="delete", path=src, result="ok")
        return RedirectResponse("/portal?m=deleted", status_code=303)


def build_app(settings: Settings) -> Starlette:
    p = Portal(settings)
    routes = [
        Route("/healthz", lambda r: PlainTextResponse("ok"), methods=["GET"]),
        Route("/portal/login", p.login_get, methods=["GET"]),
        Route("/portal/login", p.login_post, methods=["POST"]),
        Route("/portal/logout", p.logout_post, methods=["POST"]),
        Route("/portal", p.dashboard, methods=["GET"]),
        Route("/portal/upload", p.upload, methods=["POST"]),
        Route("/portal/rename", p.rename, methods=["POST"]),
        Route("/portal/move", p.move, methods=["POST"]),
        Route("/portal/delete", p.delete, methods=["POST"]),
    ]
    return Starlette(routes=routes)


def main() -> None:
    import sys

    settings = Settings.load()
    if not settings.portal_enabled:
        raise SystemExit("portal is disabled — set PORTAL_ENABLED=true to run it")
    if not settings.session_secret:
        raise SystemExit("SESSION_SECRET is required for the portal (a long random string)")
    if settings.allow_plaintext_portal:
        # The portal can't see the reverse proxy's TLS config, so it can't refuse here
        # (./docmcp.sh serve makes the DOMAIN+plaintext combo a hard error). Surface the
        # risk loudly for anyone starting the portal directly: with the Secure flag off,
        # session cookies are not encrypted.
        print(
            "[portal] WARNING: ALLOW_PLAINTEXT_PORTAL=true — session cookies are NOT marked "
            "Secure and travel in cleartext over plain HTTP. Use only on a trusted/VPN "
            "network; for HTTPS unset ALLOW_PLAINTEXT_PORTAL and front the portal with TLS.",
            file=sys.stderr,
        )
    app = build_app(settings)
    import uvicorn

    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port, log_level="warning")


if __name__ == "__main__":
    main()
