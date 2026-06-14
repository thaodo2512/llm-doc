# Documentation MCP Server

A self-hosted [MCP](https://modelcontextprotocol.io) server that exposes a documentation corpus
to coding agents (e.g. OpenAI Codex) over **Streamable HTTP**, with per-user
**bearer-token auth** and **path-prefix RBAC**. An offline ingestion pipeline turns mixed raw
sources (PDFs, Office docs, HTML, Markdown, source code, and any other text file) into a curated Markdown doc store plus a
search index. Primary retrieval is **keyword/full-text search** (ripgrep or SQLite FTS5); an
optional **vector** layer (Qdrant + a vendored, fully-offline ONNX embedder) is built but **off by default**.

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
| `read_doc(path, start_line?, end_line?)` | `{path,content,total_lines,truncated}` | denies paths outside your prefixes; `truncated=true` when the result is clipped by the size/line caps (a full read of a large doc, or a too-wide range) — request a narrower line range |
| `semantic_search(query, limit=10)` | `[{path,line,snippet,score}]` | disabled unless `ENABLE_VECTOR=true` |

All tools are filtered to the caller's `allowed_prefixes`; `read_doc` *denies* (not silently empties)
a disallowed path. Logical paths are rooted at the doc store and start with `/`.

## Get the code (clone with Git LFS)

This repo ships **~530 MB of Git LFS data**: the vendored offline Docling/RapidOCR models under
`models/` (plus any binary docs under `raw/`). **You must pull the actual LFS objects, not just the
pointer stubs** — otherwise the ingest image bakes in ~130-byte pointer text instead of the real
models and ingestion fails with a cryptic `JSONDecodeError: Expecting value: line 1 column 1 (char 0)`
on PDFs with tables/OCR.

```bash
# 1) Install git-lfs ONCE per machine, BEFORE cloning:
#    apt-get install -y git-lfs   |   dnf install git-lfs   |   brew install git-lfs
git lfs install

# 2) Clone — with git-lfs installed, the LFS objects download automatically:
git clone <repo-url> && cd <repo>

# 3) Already cloned (or git-lfs was installed late)? Materialize everything:
git lfs pull
```

> **Only changing docs, the frontend, or Python/bash logic?** You don't need the ~530 MB of
> models — only building the **ingest** or **server-vector** images does. Clone without the LFS
> payload: `GIT_LFS_SKIP_SMUDGE=1 git clone <repo-url>`, and run `git lfs pull` (or
> `./docmcp.sh models --repair`) later, only if/when you build those images. Keeps most
> contributions lightweight and avoids burning the repo's GitHub LFS bandwidth quota.

**Verify the clone is complete** — do this on every machine, especially servers:

```bash
./docmcp.sh models                # checks every models/** file against its committed LFS
                                  #   pointer (catches pointers, empty AND truncated files)
./docmcp.sh models --repair       # broken? re-materialize from Git LFS in place
```

Or by hand:

```bash
git lfs ls-files | grep ' - '     # MUST print nothing   ( - = pointer, * = real object )
du -sh models                     # ~530 MB if real; a few KB means pointers only
head -c 30 models/docling-project--docling-models/config.json; echo
                                  # real JSON ('{ ...'), NOT 'version https://git-lfs...'
```

If `git lfs pull` errors or stalls — e.g. GitHub's **LFS bandwidth quota** is exceeded, or a flaky
connection leaves a **partial** pull (some models real, some still pointers) — copy a known-good
`models/` from a machine that already has it, bypassing LFS entirely:

```bash
rsync -av --progress models/ user@host:<repo>/models/
```

> `./docmcp.sh build`/`ingest` **preflight** this: every `models/**` file is verified against the
> size in its committed LFS pointer (catching pointers, empty files, and truncated downloads), and
> anything broken is **auto-repaired from Git LFS** before the build (`LFS_AUTO_REPAIR=false` in
> `.env` makes it a hard stop instead). `ingest` also refuses to run against an image that baked in
> broken models, and `./docmcp.sh doctor` reports both — so a partial clone fails fast (or heals
> itself) instead of producing a broken ingest.

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
uv run pytest tests/unit              # fast test suite (no torch/Qdrant)
```

## Deploy to a Linux server (x86_64)

Clone the repo on the target host **with Git LFS** ([Get the code](#get-the-code-clone-with-git-lfs) —
the models are LFS data, and a pointer-only clone breaks ingest), then run the same helper — Docker is
the only dependency:

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
  `http://<server-ip>/mcp`. To publish on a non-default port, set `HTTP_PORT=8080` →
  `http://<server-ip>:8080/mcp` (use `HTTP_PORT`, **not** `BIND_PORT` or `DOMAIN=:port`). Bearer
  tokens are **not** encrypted on the wire, so use this **only on a trusted private network** you
  control (e.g. reachable solely over VPN).
- **Public / untrusted network (HTTPS):** set `DOMAIN=docs-mcp.company.internal` (Caddy serves
  automatic HTTPS on 443; `:80` redirects) **and** `HTTP_BIND=0.0.0.0`. Needs the hostname
  resolvable/reachable for ACME (or a DNS-01 setup).
- **Local only:** comment both out — `HTTP_BIND` defaults to loopback and Caddy serves plain HTTP there.

`./docmcp.sh serve` refuses to publish plaintext off loopback unless you either set a `DOMAIN`
(HTTPS) or explicitly opt in with `ALLOW_PLAINTEXT_HTTP=true` — so cleartext on the network is always
a conscious choice, never a default accident.

### Port model

Clients only ever talk to **Caddy** on the one published port. The app's `8080` lives **inside** the
container — so to change the port users connect to, set `HTTP_PORT`, **not** `BIND_PORT`.

```
  CLIENT (Codex laptop)
     │
     │  http(s)://<server-ip>:PORT/mcp      ·  PORT = HTTP_PORT (or HTTPS_PORT)
     ▼
  ┌── Caddy ─ the ONLY port exposed to the network ───────────────────────────────
  │     binds interface ......  HTTP_BIND               (0.0.0.0 in the VPN profile)
  │     publishes port .......  HTTP_PORT / HTTPS_PORT  (default 80 / 443)
  │     HTTP vs HTTPS ........  DOMAIN=<hostname> → automatic HTTPS, else plain HTTP
  └──┬────────────────────────────────────────────────────────────────────────────
     │  reverse_proxy  →  docs-mcp:8080      (fixed, not configurable)
     ▼
  ┌── docs-mcp ─ FastMCP app (internal only, NOT published) ───────────────────────
  │     app listener .........  BIND_HOST:BIND_PORT = 0.0.0.0:8080
  │     reachable only by Caddy; BIND_* are PINNED on Docker
  │     (they affect only a bare `uv run docmcp-server`, never the container)
  └────────────────────────────────────────────────────────────────────────────────
```

| What you want to change | Knob | Default | Example |
|---|---|---|---|
| Port clients connect to | `HTTP_PORT` / `HTTPS_PORT` | `80` / `443` | `HTTP_PORT=8080` → `http://<ip>:8080/mcp` |
| Which interface it's on | `HTTP_BIND` | `0.0.0.0` (VPN) | `127.0.0.1` = local-only |
| Plain HTTP vs. HTTPS | `DOMAIN` | unset → HTTP | `DOMAIN=docs.company.internal` → HTTPS |
| Caddy → app upstream | *(fixed)* | `docs-mcp:8080` | — |
| In-container app port | `BIND_HOST`:`BIND_PORT` | `0.0.0.0:8080` | **pinned on Docker**; bare `uv run` only |

> Don't use `DOMAIN=:<port>` to change the port — Caddy would listen on a container port that isn't
> published, so clients couldn't reach it. `./docmcp.sh serve` rejects that. Use `HTTP_PORT`.

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

### Optional vector (semantic) search — fully offline

`semantic_search` lets an agent retrieve by *meaning*, not just keywords. It is **off by
default** and, when on, runs **entirely on-prem with no external API**: embeddings come from
a vendored ONNX model (`models/bge-small-en-v1.5`, English, 384-dim) run in-process via
onnxruntime, and Qdrant is a local container. `EMBED_BACKEND=openai` is a legacy online
alternative (opt-in: install `.[vector,vector-openai]`, set `OPENAI_API_KEY`).

```bash
# in .env:  ENABLE_VECTOR=true        (EMBED_BACKEND=local is the default — no API key)
./docmcp.sh build server-vector       # serving image WITH the offline embedder + qdrant-client
./docmcp.sh ingest --full             # embeds chunks into Qdrant (offline)
./docmcp.sh serve                     # runs docs-mcp from the server-vector image + starts qdrant
```

Query embedding happens **inside the server process** (which is why vector serving uses the
`server-vector` image — the slim default has no embedder). Switching the embedding
backend/model/dim requires a full re-ingest (the Qdrant collection is rebuilt at the new
vector size). When `ENABLE_VECTOR=false` (default), `semantic_search` returns a clear disabled
error and neither Qdrant nor any embedder is contacted.

### Models (vendored — no download needed)

The Docling models (~600 MB: layout, table, and OCR) are **vendored in the repo**
under `models/` via Git LFS and copied into the `ingest` image at build time. So the
build needs **no model downloads** — a clone pulls the models with the repo, and
`HF_HUB_OFFLINE=1` keeps both build and runtime fully offline for models.

> The first `git clone` pulls these LFS objects — **verify they materialized**
> ([Get the code](#get-the-code-clone-with-git-lfs)); a partial pull bakes pointer stubs and breaks
> ingest. The image installs **CPU-only torch** (no proprietary NVIDIA CUDA wheels — smaller, fully OSS).

## Issuing tokens

`tokens.json` maps an opaque token to a user and the path prefixes they may read; a token may
also reference **groups** (named prefix sets defined in `groups.json`):

```json
// groups.json:  { "firmware": ["/team-fw"], "public": ["/public"] }
{
  "tok_bob_xxx":   { "user": "bob",   "allowed_prefixes": ["/public", "/team-fw"] },
  "tok_alice_xxx": { "user": "alice", "groups": ["firmware"] }   // resolves to /team-fw
}
```

Mint, list, revoke, rotate (each reloads the running server automatically):

```bash
./docmcp.sh token alice /public /team-fw   # mint a scoped token (a scope is REQUIRED)
./docmcp.sh token bob --group firmware      # mint via a group (inherits the group's prefixes)
./docmcp.sh token admin --all               # whole corpus (admin/break-glass; never the default)
./docmcp.sh token-list                       # show tokens (prefixes, groups, expiry, who minted)
./docmcp.sh token-rm tok_alice_xxxx          # revoke one token  (or: token-rm alice → all of alice's)
./docmcp.sh token-rotate alice               # mint a fresh token with alice's scope; revoke the old
```

Manage groups and verify/audit access:

```bash
./docmcp.sh group firmware /team-fw          # define/update a group (group-list, group-rm)
./docmcp.sh access-check alice /team-fw/x.md  # → ALLOW/DENY (resolves groups + RBAC)
./docmcp.sh audit                            # recent token create/revoke/rotate events
```

### Recipes: group tokens, write access, group membership

A token's effective scope is **its explicit read prefixes + every prefix of every group it
names**, plus an optional **`--write`** (portal upload) scope — all combinable on one token and
reloaded live. The three most common tasks:

**1. Mint a token from a group ("group token").** Define the group once, then issue tokens that
inherit its prefixes — edit the group later and every token that names it follows automatically:

```bash
./docmcp.sh group firmware /team-fw /team-fw-shared    # define/extend the group (read prefixes)
./docmcp.sh token bob --group firmware                 # bob inherits /team-fw + /team-fw-shared
./docmcp.sh token bob --group firmware --group public  # several groups → union of their prefixes
./docmcp.sh token bob /public --group firmware         # mix explicit prefixes with a group
```

**2. Grant write (upload) access to a user.** `--write <prefix>` adds an upload scope on top of
read scope; repeat it for several folders. Uploads land in `raw/` only (ingested by the
`schedule`); `docs-mcp` stays read-only. The write scope is usable only once the portal is enabled
(next section):

```bash
./docmcp.sh token alice /team-fw --write /team-fw            # read + write /team-fw
./docmcp.sh token alice --group firmware --write /team-fw    # group read + write
./docmcp.sh token alice /docs --write /docs --write /drafts  # write to two folders
```

**3. Add a user to a group.** Membership is expressed on the user's *token* — a member is anyone
holding a token that names the group. `token-rotate` keeps the *same* scope (so it can't add a
group); instead revoke the old token, then mint a fresh one that names the group:

```bash
./docmcp.sh token-rm carol                        # revoke carol's old token(s) first — token-rm <user> = ALL of them
./docmcp.sh token carol /public --group firmware  # mint fresh: /public + 'firmware' membership
./docmcp.sh access-tree                           # verify: groups → folders → members
./docmcp.sh access-check carol /team-fw/x.md      # → ALLOW  (firmware ⇒ /team-fw)
```

Prefer editing `tokens.json` directly? Add the name to that token's `"groups": [ … ]` array and
save the file **atomically** (temp + `mv`); the server reloads on its mtime change.

### Optional: upload/manage portal

A browser portal lets non-technical teammates **publish docs without git**. It is a separate
service that writes **only** to `raw/` (the cron `schedule` ingests it); `docs-mcp` stays
read-only. Grant write access with `--write`, enable it, and start:

```bash
./docmcp.sh token alice /team-fw --write /team-fw   # read + write /team-fw
# .env: PORTAL_ENABLED=true, ALLOW_PLAINTEXT_PORTAL=true (VPN) or DOMAIN=<host> (HTTPS)
./docmcp.sh schedule 5m && ./docmcp.sh serve        # portal at <server>/portal
```

Full security model + operations in **[`docs/PORTAL.md`](docs/PORTAL.md)**.

Keep `tokens.json` out of VCS and readable only by the server (it is bind-mounted read-only). The
server **reloads it automatically** when the file's mtime changes, so `./docmcp.sh token`/`token-rm`
take effect on the next request without a restart (the helper also restarts defensively). For a
manual edit, write the file **atomically** (temp + `mv`) so a reload never sees a half-written file.
An optional `"expires_at": <epoch>` per token is honored. Tokens are compared in constant time and never logged.

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
| …with a custom server port | `http://10.0.0.5:8080/mcp` |
| Production / public network | `https://docs-mcp.company.internal/mcp` |

The URL's port follows the server's `HTTP_PORT`/`HTTPS_PORT` (see [Port model](#port-model)): the
defaults (80/443) need no `:port`, while a server set to `HTTP_PORT=8080` means clients use
`http://<ip>:8080/mcp`. For non-local URLs, ensure the server's `ALLOWED_HOSTS` includes the exact
hostname or IP Codex uses (`10.0.0.5`, `docs-mcp.company.internal`, etc.) — **host/IP only, no port**.
Caddy forwards the original `Host` header through to the app.

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
  the token exists in `tokens.json` (the server reloads on the file's mtime change — mint/revoke take
  effect on the next request, no restart needed).
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

## Using the docs from Codex

Once connected, just ask in natural language — the agent calls `list_docs` → `search_docs`
→ `read_doc` and **cites the doc path** it used. Search is keyword-first, so name the
**exact terms** (commands, config keys, error strings, symbols, spec numbers).

```text
Use the docs MCP server. Find the firmware flashing runbook and summarize the steps.
Search our docs for DEPLOY_TOKEN and cite the doc path and line.
List the docs I can access under /team-fw.
What does our PLDM spec say about completion codes? Cite the spec path.
Open /team-fw/flashing.md lines 1–40.
```

Tips for good answers:

- **Be specific** — `search_docs` matches literal terms, so "search for `E_FLASH_TIMEOUT`"
  beats "find the flashing error".
- **Ask for the citation** ("cite the path and line") so you can verify against the source.
- If a read says **`truncated=true`**, ask for a narrower line range.
- You only ever see docs your **token's prefixes** allow — `list_docs` is pre-filtered.

If you installed the bundled **skills** (`clients/skills/`), these trigger automatically:
`docs` (find/cite), `doc-find` (locate a doc/section from a *fuzzy, half-remembered*
description — *"I remember something about multipart transfer…"* — by expanding it into
the literal terms the corpus uses), `doc-report` (a terminal inventory — *"what docs do we
have?"*), and `doc-html-report` (a shareable HTML report). Writing docs that retrieve well
is its own skill — see **[`docs/AUTHORING.md`](docs/AUTHORING.md)**.

## Configuration

Server (see `.env.example`): `DOC_ROOT`, `DOCSTORE_ROOT`, `SOURCE_DIRS`, `BIND_HOST`/`BIND_PORT`,
`TOKENS_FILE`, `SEARCH_BACKEND` (`ripgrep`|`fts5`), `FTS5_DB`, `ENABLE_VECTOR`, `QDRANT_URL`,
`OPENAI_API_KEY`, `OPENAI_EMBED_MODEL`, `EMBED_CHUNK_TOKENS`, `ALLOWED_ORIGINS`, `ALLOWED_HOSTS`,
`TOKEN_TTL`, `LOG_REQUESTS`.

- **`DOCSTORE_ROOT`** (default = `DOC_ROOT`'s parent, e.g. `/srv/docs`): where the index, manifest,
  `ingest-status.json`, lock, and FTS5 db live — **outside** `DOC_ROOT` so they're never `read_doc`-able.
  `DOC_ROOT` must be a strict subdirectory of it.
- **`LOG_REQUESTS`** (default `true`): emit one structured JSON access-log line per tool call (user,
  tool, prefix count, path, result size, ms — never the token/content). Set `false` to disable.

Network exposure (consumed by `docmcp.sh` / Caddy — see the [profiles above](#deploy-to-a-linux-server-x86_64)):
`HTTP_BIND` (host interface the plaintext listener binds to), `HTTP_PORT`/`HTTPS_PORT` (client-facing
published port numbers; default 80/443), `DOMAIN` (a hostname switches Caddy to automatic HTTPS), and
`ALLOW_PLAINTEXT_HTTP` (conscious opt-in to publish plaintext off loopback on a trusted/VPN network).
Resource bounds (DoS guards): `MAX_SEARCH_LIMIT`, `MAX_READ_BYTES`, `MAX_READ_LINES`.

> **Docker vs. bare run:** `BIND_HOST`/`BIND_PORT` set the in-container app listener and are **pinned**
> by `docker-compose.yml` on the Docker path (editing them in `.env` only affects a bare `uv run
> docmcp-server`). To change the **client-facing** port on Docker, set `HTTP_PORT` — not `BIND_PORT`.
> Note on `.env` and bare compose: the services load the repo-root `.env` into the **containers** via
> `env_file`, so in-container settings (`ALLOWED_HOSTS`, `SESSION_SECRET`, …) **do** apply under a bare
> `docker compose`. But host-side **port/bind interpolation** — `${HTTP_BIND}`/`${HTTP_PORT}`/`${DOMAIN}`
> in Caddy's `ports:` — is read only from `docker/.env`, not the repo-root `.env`, and the
> plaintext-exposure guard lives only in `docmcp.sh serve`. So drive the stack through `./docmcp.sh`; a
> bare `docker compose up` skips both the port vars and that safety check.

## Testing

Tests are split into `tests/unit/` (fast — no torch/Qdrant) and `tests/integration/`
(live server, Docling, Qdrant); shell tests live in `tests/shell/`.

```bash
uv run pytest tests/unit          # fast default — unit suite, no torch/Qdrant
uv run pytest                     # full suite (unit + integration)
uv run pytest -m "not docling and not vector"   # everything except Docling/torch + Qdrant
uv run pytest tests/unit/test_auth.py::test_invalid_token_returns_none   # single test
bash tests/shell/test_deploy_env.sh             # deploy-wizard .env helpers (pure bash)
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

## License

Licensed under the Apache License, Version 2.0 — see [`LICENSE`](LICENSE). Copyright 2026 The docmcp Authors.
