# Documentation MCP Server

A self-hosted [MCP](https://modelcontextprotocol.io) server that exposes a documentation corpus
to coding agents (e.g. OpenAI Codex) over **Streamable HTTP**, with per-user
**bearer-token auth** and **path-prefix RBAC**. An offline ingestion pipeline turns mixed raw
sources (PDFs, Office docs, HTML, Markdown, source code, and any other text file) into a curated Markdown doc store plus a
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

## Quick start (Docker + `./docmcp.sh`)

The **only** thing you need on the host is **Docker** (with the Compose plugin) — no Python,
`uv`, or ripgrep. The `./docmcp.sh` helper runs everything in containers, so it's the easy path
even for non-developers:

```bash
./docmcp.sh setup                          # build the image; create .env + tokens.json (admin token)
./docmcp.sh add /path/to/your/docs         # stage documents into raw/
./docmcp.sh ingest                         # build the searchable store (first run builds the ingest image)
./docmcp.sh serve                          # start the server + reverse proxy (background)
./docmcp.sh test                           # verify it answers (list_docs / read_doc)
./docmcp.sh token alice /public /team-fw   # mint a scoped bearer token
./docmcp.sh status                         # services, URL, index summary
./docmcp.sh stop                           # stop (your ingested store is kept)
./docmcp.sh schedule 30m                   # (optional) auto re-ingest on a cron schedule
```

`setup` builds the slim **server** image right away; the heavier **ingest** image (Docling +
tree-sitter) is built the first time you run `./docmcp.sh ingest`. The server is reachable at
**`http://localhost/mcp`** through Caddy (the only published port); set `DOMAIN=docs.company.internal`
in `.env` for automatic HTTPS. Run `./docmcp.sh help` for the full command list.

## Document corpus (`raw/`, version-controlled via Git LFS)

The `raw/` source corpus is tracked in git so nothing is silently overwritten/lost.
Binary formats (PDF/Office/images/archives) go through **Git LFS**; Markdown/text stay
as normal diffable git (see `.gitattributes`). One-time per machine:

```bash
# install git-lfs: apt-get install -y git-lfs | dnf install git-lfs | brew install git-lfs
git lfs install --local
```

Add docs and version them: `./docmcp.sh add /path/to/docs` then `git add raw/ && git commit`.
**Note:** pushing LFS objects requires an LFS-capable remote (GitHub/GitLab/self-hosted).
`.env`, `tokens.json`, and the built store (`var/`) remain ignored.

## Develop without Docker (optional)

To hack on the code itself you can run it on the host with [uv](https://docs.astral.sh/uv/)
(Python 3.11+) instead of Docker:

```bash
uv venv --python 3.11
uv pip install -e ".[dev,parse]"      # add ".[vector]" for semantic search
# ripgrep is the keyword backend: apt-get install ripgrep | brew install ripgrep
cp .env.example .env                  # for a host run set DOC_ROOT/SOURCE_DIRS to local paths (e.g. ./var/curated, ./raw)
cp tokens.json.example tokens.json    # issue per-user tokens (see below)

uv run python -m docmcp.config        # print resolved settings
uv run docmcp-ingest --full --source ./raw   # build the doc store
uv run docmcp-server                  # serve on $BIND_HOST:$BIND_PORT/mcp
uv run pytest -m "not docling"        # fast test suite
```

## Deploy to a Linux server (x86_64)

Clone the repo on the target host and run the same helper — Docker is the only dependency:

```bash
./docmcp.sh setup && ./docmcp.sh ingest && ./docmcp.sh serve
```

Two images come from one `docker/Dockerfile`: a slim **`server`** (FastMCP + ripgrep — the only
thing exposed, via Caddy) and a heavier **`ingest`** (Docling + tree-sitter [+ vector], run on
demand, never exposed). The curated store + index live in a named volume shared between them, so
`docs-mcp` binds `0.0.0.0:8080` **inside** the container but is **not published** — only Caddy
(80/443) is reachable. Set `DOMAIN=docs-mcp.company.internal` in `.env` for automatic HTTPS.

Building on an arm64 box (e.g. Apple Silicon) for an amd64 target? Build the images explicitly:

```bash
cd docker
docker buildx build --platform linux/amd64 --target server -t docs-mcp:server -f Dockerfile ..
docker buildx build --platform linux/amd64 --target ingest -t docs-mcp:ingest -f Dockerfile ..
```

Under the hood the helper just wraps `docker compose -f docker/docker-compose.yml` (`run --rm
ingest`, `up -d docs-mcp caddy`, …) — use those directly for advanced control.

### Scheduled ingest (cron)

Re-run ingestion automatically (it's incremental — unchanged files are skipped). The helper
manages a crontab entry for you:

```bash
./docmcp.sh schedule 30m      # every 30 min  (or: hourly | daily | weekly | "m h dom mon dow")
./docmcp.sh schedule          # show the current schedule
./docmcp.sh schedule off      # remove it
```

It bakes in the right `docker` PATH and logs to `var/cron-ingest.log`. The job only fires while
Docker is running (on a server `dockerd` is always up; on a Mac, Docker Desktop must be open).

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

Mint, list, and revoke tokens with the helper (each reloads the running server automatically):

```bash
./docmcp.sh token alice /public /team-fw   # mint a scoped token
./docmcp.sh token-list                     # show configured tokens
./docmcp.sh token-rm tok_alice_xxxx        # revoke one token  (or: token-rm alice → all of alice's)
```

Keep `tokens.json` out of VCS and readable only by the server (it is bind-mounted read-only). The
server loads it at startup — restart `docs-mcp` after manual edits. An optional `"expires_at": <epoch>` per token is
honored. Tokens are compared in constant time and never logged.

## Codex client setup

Point the [OpenAI Codex](https://developers.openai.com/codex) CLI at the running server. Each user
needs a **bearer token** you minted for them plus one entry in `~/.codex/config.toml`. The schema
(`url` + `bearer_token_env_var`) is the current, officially-documented one — verified against
[OpenAI's Codex MCP docs](https://developers.openai.com/codex/mcp).

**1. Operator: mint a token** scoped to the prefixes that user may read, and hand it over:

```bash
./docmcp.sh token alice /public /team-fw     # → tok_alice_xxxx
```

**2. User: register the server** — one command writes the config block:

```bash
codex mcp add docs --url http://localhost/mcp --bearer-token-env-var DOCS_MCP_TOKEN
#                       production:  --url https://docs-mcp.company.internal/mcp
```

…or hand-edit `~/.codex/config.toml` (see `clients/codex-config.example.toml`):

```toml
[mcp_servers.docs]
url = "http://localhost/mcp"             # production: "https://docs-mcp.company.internal/mcp"
bearer_token_env_var = "DOCS_MCP_TOKEN"  # the NAME of an env var — not the token itself
startup_timeout_sec = 20
```

**3. User: export the token and run Codex** (the var must exist in the shell that starts `codex` —
HTTP MCP servers get no env injection):

```bash
export DOCS_MCP_TOKEN=tok_alice_xxxx
codex
```

**4. Verify** — inside Codex run `/mcp`; you should see `docs` connected with `list_docs`,
`read_doc`, `search_docs`, `semantic_search`. Manage with `codex mcp list`, `codex mcp get docs`,
`codex mcp remove docs`.

**5. (Optional) Install the doc skills** — the repo ships Codex
[Agent Skills](https://developers.openai.com/codex/skills) under `clients/skills/`:

| Skill | What it does |
|---|---|
| `docs` | find & cite the right docs (search → read → cite) |
| `doc-report` | print a terminal inventory/overview of the docs |
| `doc-html-report` | summarize the docs into a self-contained HTML report |

Codex discovers skills as folders under `.agents/skills/`, so install the ones you want by copying
their folders in:

```bash
mkdir -p ~/.agents/skills                              # global (all projects)
cp -R clients/skills/* ~/.agents/skills/               # all of them …
# … or just one:     cp -R clients/skills/doc-html-report ~/.agents/skills/
# … or per-project:  cp -R clients/skills/doc-report .agents/skills/
```

Restart Codex, then use them via `/skills`, `$doc-report` / `$doc-html-report`, or just by asking —
Codex auto-invokes the skill whose `description` matches. (There is no `codex skill add` command —
skills are folder-based. Some Codex builds use `~/.codex/skills/` instead of `.agents/skills/`; if a
skill isn't discovered, check `codex --version` and the [skills docs](https://developers.openai.com/codex/skills).)

**Troubleshooting**
- Connected but **`Tools: (none)`** / won't initialize → older Codex build: add
  `experimental_use_rmcp_client = true` at the top of `config.toml`, or upgrade (`codex --version`).
- Still failing → use the `mcp-remote` stdio bridge (works on any Codex version):
  ```toml
  [mcp_servers.docs]
  command = "npx"
  args = ["-y", "mcp-remote", "http://localhost/mcp", "--allow-http",
          "--header", "Authorization: Bearer ${DOCS_MCP_TOKEN}"]
  env = { "DOCS_MCP_TOKEN" = "tok_alice_xxxx" }
  ```
  `--allow-http` is required for plain `http://`; drop it for an `https://` production URL.
- **4xx on connect** → ensure the server's `ALLOWED_HOSTS` includes the hostname the client uses
  (`localhost` is allowed by default) and that your reverse proxy forwards the `Authorization`
  header (Caddy does by default).

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

See `CLAUDE.md` for architecture orientation.

## License

Licensed under the Apache License, Version 2.0 — see [`LICENSE`](LICENSE). Copyright 2026 Tinh Nguyen.
