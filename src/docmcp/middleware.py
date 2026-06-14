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
            origins = [
                value.decode("latin-1")
                for key, value in scope.get("headers", [])
                if key == b"origin"
            ]
            # Reject ambiguous (multiple) Origins, or any single Origin not allowed.
            # No Origin header (typical for CLI MCP clients) passes through.
            forbidden = len(origins) > 1 or (len(origins) == 1 and origins[0] not in self.allowed)
            if forbidden:
                await PlainTextResponse("Forbidden origin", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
