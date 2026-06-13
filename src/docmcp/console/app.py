"""The console Starlette app + ``docmcp-console`` uvicorn entrypoint.

API routes (and ``/healthz``) are registered FIRST; a catch-all then serves the built
SPA from ``CONSOLE_STATIC_DIR`` (the bind-mounted ``console-ui/dist``) with an
``index.html`` history-fallback, so client-side routes like ``/tokens`` work on reload.
Everything is same-origin — no CORS.

This process runs INSIDE the ``docs-mcp:console`` container (Docker socket + repo
bind-mounted) and is published on LOOPBACK ONLY by ``cmd_console``.
"""

from __future__ import annotations

import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import FileResponse, HTMLResponse, Response
from starlette.routing import Route

from ..config import Settings
from . import commands
from .routes import Console

_NOT_BUILT = (
    "<!doctype html><meta charset=utf-8><title>docmcp console</title>"
    "<body style='font:15px system-ui;background:#0f1320;color:#e7ecf6;padding:40px'>"
    "<h1>Console UI not built</h1><p>Build the single-page app with "
    "<code>./docmcp.sh console --build</code>, then reload.</p>"
)


def _static_dir() -> Path:
    env = os.environ.get("CONSOLE_STATIC_DIR")
    if env:
        return Path(env)
    return commands.repo_root() / "console-ui" / "dist"


def build_app(settings: Settings) -> Starlette:
    c = Console(settings)
    static_dir = _static_dir()

    async def spa(request) -> Response:
        # Serve a real built file if it exists; otherwise fall back to index.html so the
        # SPA owns client-side routing. API paths never reach here (registered first).
        rel = request.path_params.get("path", "").lstrip("/")
        if rel and ".." not in rel.split("/"):
            candidate = static_dir / rel
            if candidate.is_file() and candidate.resolve().is_relative_to(static_dir.resolve()):
                return FileResponse(candidate)
        index = static_dir / "index.html"
        if index.is_file():
            return FileResponse(index)
        return HTMLResponse(_NOT_BUILT)

    routes = [
        Route("/healthz", c.healthz, methods=["GET"]),
        # auth
        Route("/api/login", c.login, methods=["POST"]),
        Route("/api/logout", c.logout, methods=["POST"]),
        Route("/api/session", c.session, methods=["GET"]),
        # reads
        Route("/api/status", c.status, methods=["GET"]),
        Route("/api/doctor", c.doctor, methods=["GET"]),
        Route("/api/inventory", c.inventory, methods=["GET"]),
        Route("/api/config", c.config, methods=["GET"]),
        Route("/api/config", c.config_set, methods=["POST"]),
        Route("/api/tokens", c.tokens, methods=["GET"]),
        Route("/api/tokens", c.token_mint, methods=["POST"]),
        Route("/api/tokens/revoke", c.token_revoke, methods=["POST"]),
        Route("/api/tokens/rotate", c.token_rotate, methods=["POST"]),
        Route("/api/groups", c.groups, methods=["GET"]),
        Route("/api/groups", c.group_define, methods=["POST"]),
        Route("/api/groups/remove", c.group_remove, methods=["POST"]),
        Route("/api/access/check", c.access_check, methods=["GET"]),
        Route("/api/access/tree", c.access_tree, methods=["GET"]),
        Route("/api/audit", c.audit, methods=["GET"]),
        Route("/api/connect", c.connect, methods=["GET"]),
        # schedule
        Route("/api/schedule", c.schedule_show, methods=["GET"]),
        Route("/api/schedule", c.schedule_set, methods=["POST"]),
        # lifecycle (jobs)
        Route("/api/lifecycle/build", c.build, methods=["POST"]),
        Route("/api/lifecycle/ingest", c.ingest, methods=["POST"]),
        Route("/api/lifecycle/serve", c.serve, methods=["POST"]),
        Route("/api/lifecycle/stop", c.stop, methods=["POST"]),
        Route("/api/lifecycle/backup", c.backup, methods=["POST"]),
        Route("/api/wizard/apply", c.wizard_apply, methods=["POST"]),
        # jobs
        Route("/api/jobs/{job_id}", c.job_status, methods=["GET"]),
        Route("/api/jobs/{job_id}/log", c.job_log, methods=["GET"]),
        Route("/api/jobs/{job_id}/stream", c.job_stream, methods=["GET"]),
        # SPA (catch-all, registered last)
        Route("/{path:path}", spa, methods=["GET"]),
    ]
    app = Starlette(routes=routes)
    app.state.console = c  # let tests inject a fake runner; harmless in production
    return app


def main() -> None:
    settings = Settings.load()
    if not settings.session_secret:
        raise SystemExit(
            "SESSION_SECRET is required for the console (./docmcp.sh console generates one)"
        )
    app = build_app(settings)
    import uvicorn

    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port, log_level="warning")


if __name__ == "__main__":
    main()
