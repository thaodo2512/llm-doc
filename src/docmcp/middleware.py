"""ASGI middleware for HTTP-transport hardening.

FastMCP v3 does not wire the MCP SDK's transport-security (DNS-rebinding)
protection, so we enforce Origin validation here, paired with Starlette's
`TrustedHostMiddleware` for the Host header (configured in server.py).
"""

from __future__ import annotations

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class OriginValidationMiddleware:
    """Reject browser cross-origin requests (DNS-rebinding protection).

    A request carrying an `Origin` header that is not in `allowed_origins` is
    refused with 403. Requests with no `Origin` (the norm for CLI MCP clients
    such as Codex) pass through.
    """

    def __init__(self, app: ASGIApp, allowed_origins: list[str] | None = None):
        self.app = app
        self.allowed = set(allowed_origins or [])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            origin = None
            for key, value in scope.get("headers", []):
                if key == b"origin":
                    origin = value.decode("latin-1")
                    break
            if origin is not None and origin not in self.allowed:
                response = PlainTextResponse("Forbidden origin", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
