# Documentation MCP Server

A self-hosted [MCP](https://modelcontextprotocol.io) server that exposes a documentation corpus
to coding agents (e.g. OpenAI Codex) over **Streamable HTTP**, with per-user
**bearer-token auth** and **path-prefix RBAC**. An offline ingestion pipeline turns mixed raw
sources (PDFs, Office docs, HTML, Markdown, source code, and any other text file) into a curated Markdown doc store plus a
search index. Primary retrieval is **keyword/full-text search** (ripgrep or SQLite FTS5); an
optional **vector** layer (Qdrant + OpenAI embeddings) is built but **off by default**.

```
Codex (laptop) --VPN--> internal network --HTTP + bearer token (raw IP)--> [ Caddy ] --> docs-mcp (FastMCP)
   (untrusted/public network instead? --HTTPS via Caddy on a hostname)        |         |
                                                                        keyword     vector (optional)
                                                                              \        /
                                                              curated doc store  <-- ingest (Docling, tree-sitter) <-- raw sources
```

## Tools

| Tool | Returns | Notes |
|------|---------|-------|
| `list_docs(path="")` | `[{path,title,type,bytes,mtime}]` | index entries under `path` |
| `search_docs(query, limit=10)` | `[{path,line,snippet,score}]` | keyword (ripgrep/FTS5) |
| `read_doc(path, start_line?, end_line?)` | `{path,content,total_lines,truncated}` | denies paths outside your prefixes; `truncated=true` when the read is capped (page with a line range) |
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
**`http://<server-ip>/mcp`** through Caddy. The shipped `.env` default is the **internal-network
profile**: plain HTTP reachable by the server's **raw IP** over a trusted network (e.g. VPN) — set
`ALLOWED_HOSTS` to your server's IP and clients connect with no TLS cert to install. Bearer tokens
travel unencrypted, so keep it on a network you trust. For an untrusted/public network set
**`DOMAIN=docs.company.internal`** instead (Caddy then serves automatic HTTPS on 443); to keep it to
one machine, comment out `HTTP_BIND`/`ALLOW_PLAINTEXT_HTTP` for loopback-only HTTP. See the
[network-exposure profiles](#deploy-to-a-linux-server-x86_64) below. Run `./docmcp.sh help` for all commands.

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
`docs-mcp` binds `0.0.0.0:8080` **inside** the container but is **not published** — only Caddy is
reachable. Choose how it's exposed in `.env` (pick one profile):

- **Internal network over VPN (default, simplest):** reach the server by its **raw IP** over plain
  HTTP — nothing to install on client laptops (no TLS cert to trust). Set `HTTP_BIND=0.0.0.0` and
  `ALLOW_PLAINTEXT_HTTP=true`, add the server's IP to `ALLOWED_HOSTS`, and point clients at
  `http://<server-ip>/mcp`. Bearer tokens are **not** encrypted on the wire, so use this **only on a
  trusted private network** you control (e.g. reachable solely over VPN).
- **Public / untrusted network (HTTPS):** set `DOMAIN=docs-mcp.company.internal` (Caddy serves
  automatic HTTPS on 443; `:80` redirects) **and** `HTTP_BIND=0.0.0.0`. Needs the hostname
  resolvable/reachable for ACME (or a DNS-01 setup).
- **Local only:** comment both out — `HTTP_BIND` defaults to loopback and Caddy serves plain HTTP there.

`./docmcp.sh serve` refuses to publish plaintext off loopback unless you either set a `DOMAIN`
(HTTPS) or explicitly opt in with `ALLOW_PLAINTEXT_HTTP=true` — so cleartext on the network is always
a conscious choice, never a default accident.

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

### Models (vendored — no download needed)

The Docling models (~600 MB: layout, table, and OCR) are **vendored in the repo**
under `models/` via Git LFS and copied into the `ingest` image at build time. So the
build needs **no model downloads** — a clone pulls the models with the repo, and
`HF_HUB_OFFLINE=1` keeps both build and runtime fully offline for models.

> The first `git clone` pulls ~600 MB of LFS objects — clone once and keep it. The
> image installs **CPU-only torch** (no proprietary NVIDIA CUDA wheels — smaller,
> fully OSS).

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

Point the [OpenAI Codex](https://developers.openai.com/codex) CLI or IDE extension at the running
server with Codex's native **Streamable HTTP MCP** support. Each user needs:

- the MCP URL, ending in `/mcp`;
- a bearer token scoped to the prefixes they may read;
- one `[mcp_servers.docs]` entry in `~/.codex/config.toml` or a trusted project `.codex/config.toml`.

Keep the token in an environment variable. Do not paste bearer tokens into `config.toml`.

Common URLs:

| Use case | URL |
|---|---|
| Same machine as the server | `http://localhost/mcp` |
| Trusted VPN/LAN by raw IP | `http://10.0.0.5/mcp` |
| Production / public network | `https://docs-mcp.company.internal/mcp` |

For non-local URLs, ensure the server's `ALLOWED_HOSTS` includes the exact hostname or IP Codex uses
(`10.0.0.5`, `docs-mcp.company.internal`, etc.). Caddy forwards the original `Host` header through to
the app.

**1. Operator: mint a scoped token** and send it to the user through a secure channel:

```bash
./docmcp.sh token alice /public /team-fw --expires 90d
```

**2. User: export the token in the shell that starts Codex:**

```bash
export DOCS_MCP_TOKEN=tok_alice_xxxx
```

**3. User: register the MCP server** with the native HTTP client:

```bash
codex mcp add docs --url http://10.0.0.5/mcp --bearer-token-env-var DOCS_MCP_TOKEN
```

Replace the URL with `http://localhost/mcp` for a same-machine setup or
`https://docs-mcp.company.internal/mcp` for production.

You can also hand-edit `~/.codex/config.toml` (see `clients/codex-config.example.toml`):

```toml
[mcp_servers.docs]
url = "http://10.0.0.5/mcp"
bearer_token_env_var = "DOCS_MCP_TOKEN"
startup_timeout_sec = 20
```

**4. User: start Codex and verify the connection:**

```bash
codex
```

Inside Codex, run `/mcp`. The `docs` server should show these tools:
`list_docs`, `search_docs`, `read_doc`, and `semantic_search`.

Useful management commands:

```bash
codex mcp list
codex mcp get docs
codex mcp remove docs
```

**5. (Optional) Install the doc skills** — the repo ships Codex
[Agent Skills](https://developers.openai.com/codex/skills) under `clients/skills/`:

| Skill | What it does |
|---|---|
| `docs` | find & cite the right docs (search → read → cite) |
| `doc-report` | print a terminal inventory/overview of the docs |
| `doc-html-report` | export authorized docs into a self-contained HTML report |

Codex discovers skills as folders under `.agents/skills/`, so install the ones you want by copying
their folders in:

```bash
mkdir -p ~/.agents/skills                              # global (all projects)
cp -R clients/skills/* ~/.agents/skills/               # all of them …
# … or just one:     cp -R clients/skills/doc-html-report ~/.agents/skills/
# … or repo-scoped:  mkdir -p .agents/skills && cp -R clients/skills/* .agents/skills/
```

Restart Codex, then use them via `/skills`, `$doc-report` / `$doc-html-report`, or just by asking —
Codex auto-invokes the skill whose `description` matches. Skills are folder-based; if a skill isn't
discovered, confirm it is under `.agents/skills` (repo-scoped) or `~/.agents/skills` (user-scoped),
then restart Codex and check the [skills docs](https://developers.openai.com/codex/skills).

**Troubleshooting**
- **401 Unauthorized** → verify `DOCS_MCP_TOKEN` is exported in the shell that starts Codex and that
  the server was restarted after the token was minted or revoked.
- **403 Forbidden origin** → browser-style clients must use an `Origin` listed in `ALLOWED_ORIGINS`.
  Codex CLI normally sends no `Origin` header.
- **400/4xx host errors** → add the exact hostname/IP in the MCP URL to `ALLOWED_HOSTS`, then restart
  `docs-mcp`.
- **Connected but tools do not appear** → run `codex --version` and upgrade Codex. If native HTTP MCP
  still misbehaves, use the `mcp-remote` stdio bridge:
  ```toml
  [mcp_servers.docs]
  command = "npx"
  args = ["-y", "mcp-remote", "http://10.0.0.5/mcp", "--allow-http",
          "--header", "Authorization: Bearer ${DOCS_MCP_TOKEN}"]
  env_vars = ["DOCS_MCP_TOKEN"]  # forward from the shell; do not paste the token here
  ```
  Use your server's VPN/LAN IP; `--allow-http` is required for plain `http://` and should be removed
  for an `https://` URL.

## Configuration

Server (see `.env.example`): `DOC_ROOT`, `SOURCE_DIRS`, `BIND_HOST`/`BIND_PORT`, `TOKENS_FILE`,
`SEARCH_BACKEND` (`ripgrep`|`fts5`), `FTS5_DB`, `ENABLE_VECTOR`, `QDRANT_URL`, `OPENAI_API_KEY`,
`OPENAI_EMBED_MODEL`, `EMBED_CHUNK_TOKENS`, `ALLOWED_ORIGINS`, `ALLOWED_HOSTS`, `TOKEN_TTL`.

Network exposure (consumed by `docmcp.sh` / Caddy — see the [profiles above](#deploy-to-a-linux-server-x86_64)):
`HTTP_BIND` (host interface the plaintext `:80` listener binds to), `DOMAIN` (a hostname switches
Caddy to automatic HTTPS), and `ALLOW_PLAINTEXT_HTTP` (conscious opt-in to publish plaintext off
loopback on a trusted/VPN network). Resource bounds (DoS guards): `MAX_SEARCH_LIMIT`,
`MAX_READ_BYTES`, `MAX_READ_LINES`.

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
`Origin` header (DNS-rebinding) and an optional `ALLOWED_HOSTS` allowlist. The server itself only
speaks plain HTTP and is never published directly — it is reached through Caddy, and you match the
[network profile](#deploy-to-a-linux-server-x86_64) to your trust boundary: plain HTTP by raw IP on a
trusted/VPN network (the default, gated by the explicit `ALLOW_PLAINTEXT_HTTP` opt-in so cleartext is
never accidental), or automatic HTTPS via `DOMAIN` for an untrusted/public one — bearer tokens must
never travel plaintext on a network you don't trust. Tokens are constant-time compared, never logged,
and honor an optional `expires_at`. `read_doc`/`search_docs` are bounded (`MAX_READ_*`,
`MAX_SEARCH_LIMIT`) so an authenticated caller can't exhaust resources. Add per-token rate limiting at
the proxy (see `docker/Caddyfile`).

See `CLAUDE.md` for architecture orientation.

## License

Licensed under the Apache License, Version 2.0 — see [`LICENSE`](LICENSE). Copyright 2026 Tinh Nguyen.
