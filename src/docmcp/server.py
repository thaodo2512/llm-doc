"""FastMCP application + Streamable HTTP transport.

Builds the MCP server (auth + tools), wraps the ASGI app with Origin/Host
hardening middleware, and serves it. Bind to localhost; TLS is terminated by a
reverse proxy (see README / docker-compose).
"""

from __future__ import annotations

from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import JsonFileTokenVerifier
from .config import Settings
from .middleware import OriginValidationMiddleware
from .tools import register_tools

# Kept self-contained within the first 512 characters.
INSTRUCTIONS = (
    "Documentation server. Flow: list_docs to see the index, then "
    "search_docs with specific keywords (code symbols, config keys, exact "
    "terms), then read_doc the top hit (pass a line range for long files). "
    "Always cite the doc path you used. Paths are logical, rooted at the doc "
    "store, and start with '/'. You only see documents your token is authorized "
    "for. semantic_search is optional and may be disabled."
)

MCP_PATH = "/mcp"


def build_server(settings: Settings) -> FastMCP:
    mcp = FastMCP(
        name="docs",
        instructions=INSTRUCTIONS,
        auth=JsonFileTokenVerifier(settings.tokens_file),
    )
    register_tools(mcp, settings)
    return mcp


def build_asgi_app(settings: Settings):
    """Return the Starlette ASGI app (used by `main()` and by tests)."""
    mcp = build_server(settings)
    middleware: list[Middleware] = []
    if settings.allowed_hosts:
        middleware.append(
            Middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
        )
    middleware.append(
        Middleware(OriginValidationMiddleware, allowed_origins=settings.allowed_origins)
    )
    return mcp.http_app(path=MCP_PATH, middleware=middleware)


def main() -> None:
    import uvicorn

    settings = Settings.load()
    app = build_asgi_app(settings)
    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port, log_level="info")


if __name__ == "__main__":
    main()
