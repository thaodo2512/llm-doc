"""Console authentication: admin-token login, the pre-setup bootstrap token, signed
session cookies, and the CSRF guard.

Reuses ``portal.sessions`` (HMAC-signed stateless cookies + a per-session CSRF token)
verbatim — same crypto as the doc-upload portal. Two roles ride in the session:

* ``admin`` — issued for a whole-corpus ("--all") bearer token; may do everything.
* ``bootstrap`` — issued for the in-memory bootstrap token, valid ONLY before setup has
  minted an admin token, and ONLY for the wizard + read-only status/config. It is
  invalidated the moment an admin token exists.

Loopback-only access is enforced by the ``-p 127.0.0.1:…`` publish in ``cmd_console`` (a
container peer appears as the bridge IP, so an in-process client-IP check would misfire);
the bootstrap token is in-memory and single-purpose as defence in depth.
"""

from __future__ import annotations

import os
import secrets

from starlette.requests import Request
from starlette.responses import Response

from ..config import Settings
from ..portal import sessions
from . import reads

COOKIE = "docmcp_console"


class ConsoleAuth:
    def __init__(self, settings: Settings):
        self.settings = settings
        from ..auth import JsonFileTokenVerifier

        self.verifier = JsonFileTokenVerifier(settings.tokens_file, settings.groups_file)
        # Secure cookie unless the operator opted into plaintext (loopback dev / VPN).
        self.secure = not settings.allow_plaintext_portal
        # In-memory bootstrap token (never persisted). Passed in by cmd_console.
        self._bootstrap_token = os.environ.get("CONSOLE_BOOTSTRAP_TOKEN") or None

    # -- state ---------------------------------------------------------------
    def setup_done(self) -> bool:
        return reads.setup_done(self.settings)

    def bootstrap_active(self) -> bool:
        """Bootstrap is live only while no admin token exists yet."""
        return self._bootstrap_token is not None and not self.setup_done()

    # -- login ---------------------------------------------------------------
    async def authenticate(self, *, token: str = "", bootstrap: str = "") -> dict | None:
        """Return a new session dict for valid credentials, else ``None``.

        ``bootstrap`` is accepted only while :meth:`bootstrap_active`. A normal ``token``
        must be an admin (whole-corpus) token — scoped tokens are refused (the caller maps
        that to 403)."""
        if bootstrap:
            if self.bootstrap_active() and secrets.compare_digest(bootstrap, self._bootstrap_token or ""):
                sess = sessions.new_session("bootstrap", [], [])
                sess["role"] = "bootstrap"
                return sess
            return None
        if not token:
            return None
        access = await self.verifier.verify_token(token)
        if access is None:
            return None
        claims = access.claims
        if not reads.is_admin_claims(claims):
            return "not-admin"  # sentinel: valid token but not whole-corpus → 403
        sess = sessions.new_session(
            claims["user"], claims.get("allowed_prefixes", []), claims.get("writable_prefixes", [])
        )
        sess["role"] = "admin"
        return sess

    # -- session / cookie helpers --------------------------------------------
    def session(self, request: Request, *, allow_stale_bootstrap: bool = False) -> dict | None:
        raw = request.cookies.get(COOKIE)
        if not raw:
            return None
        sess = sessions.verify(raw, self.settings.session_secret)
        if not sess:
            return None
        # A bootstrap session dies as soon as an admin token has been minted — EXCEPT for reading
        # the very wizard job that minted it (allow_stale_bootstrap). Setup runs FIRST in the
        # deploy (it mints the admin token, flipping setup_done), so without this the live log
        # would 401 the instant setup finished — mid-deploy, while the long ingest is still going —
        # and the user would watch the stream freeze. A stale bootstrap can still ONLY read job
        # output (status/log/stream); it cannot start a new setup or reach any admin data.
        if sess.get("role") == "bootstrap" and self.setup_done() and not allow_stale_bootstrap:
            return None
        return sess

    def set_cookie(self, resp: Response, session: dict) -> None:
        resp.set_cookie(
            COOKIE,
            sessions.sign(session, self.settings.session_secret),
            httponly=True,
            samesite="strict",
            secure=self.secure,
            max_age=sessions.SESSION_TTL,
            path="/",
        )

    def clear_cookie(self, resp: Response) -> None:
        resp.delete_cookie(COOKIE, path="/", samesite="strict", secure=self.secure, httponly=True)

    # -- guards --------------------------------------------------------------
    def require(
        self, request: Request, *, admin: bool = True, allow_stale_bootstrap: bool = False
    ) -> dict | None:
        """Return the session if present and (when ``admin``) it is an admin session.
        Does NOT check CSRF — use :meth:`guard` for mutations. ``allow_stale_bootstrap`` lets a
        bootstrap session survive past setup for READ-ONLY job inspection (see :meth:`session`)."""
        sess = self.session(request, allow_stale_bootstrap=allow_stale_bootstrap)
        if not sess:
            return None
        if admin and sess.get("role") != "admin":
            return None
        return sess

    def guard(self, request: Request, csrf_header: str | None, *, admin: bool = True) -> dict | None:
        """Mutation guard: valid session + matching CSRF header. ``admin=False`` also
        admits a bootstrap session (used by the wizard during first-run)."""
        sess = self.session(request)
        if not sess:
            return None
        if admin and sess.get("role") != "admin":
            return None
        if not csrf_header or not secrets.compare_digest(csrf_header, sess.get("csrf") or ""):
            return None
        return sess
