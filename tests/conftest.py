"""Shared pytest fixtures: a temp doc store ingested from tests/fixtures/raw."""

from __future__ import annotations

from pathlib import Path

import pytest

from docmcp.config import Settings

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
