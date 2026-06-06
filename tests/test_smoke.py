"""End-to-end smoke test: boot the server, drive tools over authenticated HTTP.

Exercises the full DI path (get_access_token over Streamable HTTP) that unit
tests can't reach. Extended with search assertions in M3.
"""

from __future__ import annotations

import asyncio
import socket

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from docmcp.server import build_asgi_app


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class LiveServer:
    """Run the ASGI app on an ephemeral port inside the test event loop."""

    def __init__(self, settings):
        self.settings = settings
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "LiveServer":
        config = uvicorn.Config(
            build_asgi_app(self.settings), host="127.0.0.1", port=self.port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.should_exit = True
        await self._task

    def client(self, token: str) -> Client:
        return Client(
            StreamableHttpTransport(self.url, headers={"Authorization": f"Bearer {token}"})
        )


async def test_smoke_list_and_read(ingested):
    async with LiveServer(ingested) as srv:
        async with srv.client("tok_alice_full") as client:
            tool_names = {t.name for t in await client.list_tools()}
            assert {"list_docs", "search_docs", "read_doc", "semantic_search"} <= tool_names

            docs = (await client.call_tool("list_docs", {"path": ""})).data
            assert any(d.path == "/public/welcome.md" for d in docs)

            content = (await client.call_tool("read_doc", {"path": "/public/welcome.md"})).data
            assert "Welcome" in content.content
            assert content.total_lines >= 3


async def test_smoke_rbac_scoped_token(ingested):
    async with LiveServer(ingested) as srv:
        async with srv.client("tok_bob_public") as client:
            docs = (await client.call_tool("list_docs", {"path": ""})).data
            assert docs and all(d.path.startswith("/public") for d in docs)

            with pytest.raises(Exception):  # ToolError surfaces as a client-side error
                await client.call_tool("read_doc", {"path": "/team-fw/design.md"})
