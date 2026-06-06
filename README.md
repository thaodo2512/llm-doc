# Internal Documentation MCP Server

A self-hosted [MCP](https://modelcontextprotocol.io) server that exposes a company's internal
documentation to coding agents (e.g. OpenAI Codex) over Streamable HTTP, with per-user bearer-token
auth and path-prefix RBAC. An offline ingestion pipeline turns mixed raw sources (Git repos, PDFs,
slide decks, HTML, Markdown) into a curated Markdown doc store plus a search index.

> Full operator documentation (deployment, token issuance, scheduled ingest, reverse proxy, Codex
> client setup) is filled in at milestone M7. This stub exists so the package builds.

## Quick start (local dev)

```bash
uv venv --python 3.11
uv pip install -e ".[dev]"          # add ".[parse]" for ingestion, ".[vector]" for semantic search
cp .env.example .env                # then edit paths/tokens
uv run python -m docmcp.config      # print resolved settings
```

See `internal-docs-mcp-build-brief.md` for the authoritative spec and `CLAUDE.md` for an orientation.
