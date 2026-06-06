# Internal Documentation MCP Server

A self-hosted [MCP](https://modelcontextprotocol.io) server that exposes a company's internal
documentation to coding agents (e.g. OpenAI Codex) over **Streamable HTTP**, with per-user
**bearer-token auth** and **path-prefix RBAC**. An offline ingestion pipeline turns mixed raw
sources (Git repos, PDFs, slide decks, HTML, Markdown) into a curated Markdown doc store plus a
search index. Primary retrieval is **keyword/full-text search** (ripgrep or SQLite FTS5); an
optional **vector** layer (Qdrant + OpenAI embeddings) is built but **off by default**.

```
Codex (laptop) --HTTPS + bearer token--> [ Caddy TLS proxy ] --> docs-mcp (FastMCP)
                                                                    |         |
                                                              keyword     vector (optional)
                                                                    \        /
                                                              curated doc store  <-- ingest (Docling, tree-sitter) <-- raw sources
```

## Tools

| Tool | Returns | Notes |
|------|---------|-------|
| `list_docs(path="")` | `[{path,title,type,bytes,mtime}]` | index entries under `path` |
| `search_docs(query, limit=10)` | `[{path,line,snippet,score}]` | keyword (ripgrep/FTS5) |
| `read_doc(path, start_line?, end_line?)` | `{path,content,total_lines}` | denies paths outside your prefixes |
| `semantic_search(query, limit=10)` | `[{path,line,snippet,score}]` | disabled unless `ENABLE_VECTOR=true` |

All tools are filtered to the caller's `allowed_prefixes`; `read_doc` *denies* (not silently empties)
a disallowed path. Logical paths are rooted at the doc store and start with `/`.

## Helper script (`./docmcp.sh`)

A Linux/macOS helper wraps the whole loop:

```bash
./docmcp.sh setup                 # venv + deps + .env + tokens.json (+ ripgrep check)
./docmcp.sh add /path/to/docs     # copy files/dirs into raw/
./docmcp.sh ingest --full         # build the curated doc store + index
./docmcp.sh serve                 # run the MCP server (one terminal)
./docmcp.sh test                  # exercise the running server (another terminal)
./docmcp.sh token alice /public /team-fw   # mint a scoped bearer token
./docmcp.sh status                # config + index summary
```

## Document corpus (`raw/`, version-controlled via Git LFS)

The `raw/` source corpus is tracked in git so nothing is silently overwritten/lost.
Binary formats (PDF/Office/images/archives) go through **Git LFS**; Markdown/text stay
as normal diffable git (see `.gitattributes`). One-time per machine:

```bash
# install git-lfs: apt-get install -y git-lfs | dnf install git-lfs | brew install git-lfs
git lfs install --local        # ./docmcp.sh setup does this for you
```

Add docs and version them: `./docmcp.sh add /path/to/docs` then `git add raw/ && git commit`.
**Note:** pushing LFS objects requires an LFS-capable remote (GitHub/GitLab/self-hosted).
`.env`, `tokens.json`, and the built store (`var/`) remain ignored.

## Quick start (local dev)

```bash
uv venv --python 3.11
uv pip install -e ".[dev]"            # add ".[parse]" for ingestion, ".[vector]" for semantic search
brew install ripgrep                  # keyword backend (Linux: apt-get install ripgrep)
cp .env.example .env                  # edit paths/tokens
cp tokens.json.example tokens.json    # issue per-user tokens (see below)

uv run python -m docmcp.config        # print resolved settings
uv run docmcp-ingest --full --source ./raw   # build the doc store
uv run docmcp-server                  # serve on $BIND_HOST:$BIND_PORT/mcp
```

## Deploy with Docker (x86_64 Linux)

The system ships as two images from one `docker/Dockerfile`:

- **`server`** — slim runtime you expose (FastMCP + ripgrep; no torch/Docling).
- **`ingest`** — heavier build-path image (Docling + tree-sitter + optional vector) used to
  (re)build the doc store; never exposed.

```bash
cp .env.example .env                  # configure (DOC_ROOT etc. default to /srv/docs/* inside the volume)
cp tokens.json.example tokens.json    # your tokens (bind-mounted read-only into the server)
mkdir -p raw && cp -r /path/to/internal/docs/* raw/

cd docker
# Build for the target arch (this dev box is arm64; deploy targets are amd64):
docker buildx build --platform linux/amd64 --target server  -t internal-docs-mcp:server  -f Dockerfile ..
docker buildx build --platform linux/amd64 --target ingest  -t internal-docs-mcp:ingest  -f Dockerfile ..

docker compose run --rm ingest --full   # build the doc store into the shared volume
docker compose up -d docs-mcp caddy      # serve; Caddy is the only exposed port
```

`docs-mcp` binds `0.0.0.0:8080` **inside** the container but is **not published** — only Caddy
(80/443) is reachable, preserving the "bind localhost, expose only via reverse proxy" rule. Set
`DOMAIN=docs-mcp.company.internal` in `.env` to enable Caddy's automatic HTTPS.

### Scheduled ingest (cron)

Re-run ingestion when docs change. Ingestion is incremental (unchanged sources are skipped):

```cron
# Rebuild the internal docs store nightly at 02:30
30 2 * * *  cd /opt/internal-docs-mcp/docker && docker compose run --rm ingest --full >> /var/log/docmcp-ingest.log 2>&1
```

### Optional vector search

```bash
# in .env:  ENABLE_VECTOR=true  and  OPENAI_API_KEY=sk-...
cd docker
docker compose --profile vector up -d qdrant
docker compose run --rm ingest --full     # embeds chunks into Qdrant
docker compose up -d docs-mcp caddy
```

When `ENABLE_VECTOR=false` (default) `semantic_search` returns a clear disabled error and neither
Qdrant nor OpenAI is contacted.

### Air-gapped hosts

The `ingest` image prefetches Docling's layout/table models at build time
(`DOCLING_ARTIFACTS_PATH=/opt/docling/models`), so the host needs no internet. Set
`HF_HUB_OFFLINE=1` to forbid any Hugging Face calls. (To slim the amd64 image, install the CPU-only
torch wheel first — see the comment in `docker/Dockerfile`.)

## Issuing tokens

`tokens.json` maps an opaque token to a user and the path prefixes they may read:

```json
{
  "tok_alice_xxx": { "user": "alice", "allowed_prefixes": ["/"] },
  "tok_bob_xxx":   { "user": "bob",   "allowed_prefixes": ["/public", "/team-fw"] }
}
```

Generate a token with `python -c "import secrets; print('tok_alice_'+secrets.token_hex(16))"`. Keep
the file out of VCS and readable only by the server (it is bind-mounted read-only). The server loads
it at startup — restart `docs-mcp` after edits. An optional `"expires_at": <epoch>` per token is
honored. Tokens are compared in constant time and never logged.

## Codex client setup

Share `clients/codex-config.example.toml` and `clients/skill/SKILL.md` with colleagues. Each sets
`DOCS_MCP_TOKEN` in their environment and points Codex at `https://<host>/mcp`. The skill is
implicitly invoked when a prompt mentions internal docs/specs/runbooks.

## Configuration

Env-driven (see `.env.example`): `DOC_ROOT`, `SOURCE_DIRS`, `BIND_HOST`/`BIND_PORT`, `TOKENS_FILE`,
`SEARCH_BACKEND` (`ripgrep`|`fts5`), `FTS5_DB`, `ENABLE_VECTOR`, `QDRANT_URL`, `OPENAI_API_KEY`,
`OPENAI_EMBED_MODEL`, `EMBED_CHUNK_TOKENS`, `ALLOWED_ORIGINS`, `ALLOWED_HOSTS`.

## Testing

```bash
uv run pytest                     # full suite
uv run pytest -m "not docling"    # fast: skip Docling/torch conversion tests
uv run pytest tests/test_auth.py::test_invalid_token_returns_none   # single test
```

Vector tests auto-skip unless a Qdrant is reachable on `localhost:6333`
(`docker run -p 6333:6333 qdrant/qdrant`).

## Security

Path traversal is contained in `docstore.py` (resolve-and-contain; the only filesystem resolver).
Every tool intersects paths with the caller's `allowed_prefixes`. The HTTP transport validates the
`Origin` header (DNS-rebinding) and an optional `ALLOWED_HOSTS` allowlist; bind localhost and expose
only via the TLS reverse proxy. Add per-token rate limiting at the proxy (see `docker/Caddyfile`).

See `internal-docs-mcp-build-brief.md` for the authoritative spec and `CLAUDE.md` for orientation.
