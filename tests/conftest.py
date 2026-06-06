"""Shared pytest fixtures: a temp doc store ingested from tests/fixtures/raw."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from docmcp.config import Settings
from docmcp.server import build_asgi_app

FIXTURES = Path(__file__).parent / "fixtures"
RAW = FIXTURES / "raw"
TOKENS = FIXTURES / "tokens.json"


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        doc_root=tmp_path / "curated",
        source_dirs=[str(RAW)],
        bind_host="127.0.0.1",
        bind_port=8080,
        tokens_file=TOKENS,
        search_backend="ripgrep",
        fts5_db=tmp_path / "index.sqlite",
        enable_vector=False,
        qdrant_url="http://localhost:6333",
        openai_api_key="",
        openai_embed_model="text-embedding-3-small",
        embed_chunk_tokens=512,
        allowed_origins=[],
        allowed_hosts=[],
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def settings_factory():
    """Return the make_settings helper for tests needing custom source dirs."""
    return make_settings


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class LiveServer:
    """Run the ASGI app on an ephemeral port inside the test event loop."""

    def __init__(self, settings: Settings):
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


@pytest.fixture
def live_server():
    """Factory: `async with live_server(settings) as srv: ...`."""
    return LiveServer


@pytest.fixture
def ingested(settings: Settings) -> Settings:
    """Run a full ingest of the fixtures and return the settings pointing at it."""
    from docmcp.ingest.pipeline import run_ingest

    run_ingest(settings, full=True)
    return settings


@pytest.fixture
def ingested_fts(tmp_path: Path) -> Settings:
    """Ingest with the FTS5 backend (builds the sqlite index)."""
    from docmcp.ingest.pipeline import run_ingest

    settings = make_settings(
        tmp_path, search_backend="fts5", fts5_db=tmp_path / "index.sqlite"
    )
    run_ingest(settings, full=True)
    return settings
