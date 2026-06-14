# Operating docmcp — full reference

This is the complete operator reference: deploy profiles and ports, the `./docmcp.sh` command
set, token/group recipes, vector search, scheduled ingest, configuration, testing, the Codex
client guide, troubleshooting, and the security model. For the quick start see the
[README](../README.md); for the browser console see [CONSOLE.md](CONSOLE.md).

---

## Models & Git LFS

The repo ships **~530–600 MB of vendored offline models** under `models/` (Docling layout/table,
RapidOCR, and the optional BGE embedder) via **Git LFS**, copied into the `ingest` image at build
time so the build needs no downloads and `HF_HUB_OFFLINE=1` keeps everything offline.

**You must pull the real LFS objects, not the pointer stubs** — otherwise the ingest image bakes in
~130-byte pointer text and ingestion fails with a cryptic
`JSONDecodeError: Expecting value: line 1 column 1 (char 0)` on PDFs with tables/OCR.

```bash
# install git-lfs ONCE per machine, BEFORE cloning:
#   apt-get install -y git-lfs  |  dnf install git-lfs  |  brew install git-lfs
git lfs install
git clone <repo-url> && cd <repo>     # LFS objects download automatically
git lfs pull                          # already cloned / git-lfs installed late? materialize now
```

**Verify the clone** (do this on every machine, especially servers):

```bash
./docmcp.sh models                # checks every models/** file against its committed LFS pointer
./docmcp.sh models --repair       # broken? re-materialize from Git LFS in place

# or by hand:
git lfs ls-files | grep ' - '     # MUST print nothing  ( - = pointer, * = real object )
du -sh models                     # ~530 MB if real; a few KB means pointers only
```

If `git lfs pull` stalls (GitHub LFS bandwidth quota exceeded, or a partial pull), copy a
known-good `models/` from a machine that has it, bypassing LFS:

```bash
rsync -av --progress models/ user@host:<repo>/models/
```

`./docmcp.sh build`/`ingest` **preflight** this automatically: every `models/**` file is verified
against the size in its committed LFS pointer and anything broken is auto-repaired from Git LFS
before the build (`LFS_AUTO_REPAIR=false` in `.env` makes it a hard stop). `ingest` refuses to run
against an image that baked in broken models, and `./docmcp.sh doctor` reports both.

**Lightweight contributions:** only the **ingest** / **server-vector** images need the models.
Clone without them via `GIT_LFS_SKIP_SMUDGE=1 git clone <repo-url>` and run `git lfs pull` (or
`./docmcp.sh models --repair`) later, only if/when you build those images.

---

## The `./docmcp.sh` helper

The host needs only **Docker** (with the Compose plugin). The helper runs everything in containers.

```bash
./docmcp.sh setup                          # build the image; create .env + tokens.json (admin token)
./docmcp.sh link /path/to/your/docs        # add a docs folder — read in place, no copy (recommended)
./docmcp.sh ingest                         # build the searchable store (first run builds the ingest image)
./docmcp.sh serve                          # start the server + reverse proxy (background)
./docmcp.sh test                           # verify it answers (list_docs / read_doc)
./docmcp.sh token alice /public /team-fw   # mint a scoped bearer token
./docmcp.sh status                         # services, URL, index summary
./docmcp.sh stop                           # stop (your ingested store is kept)
./docmcp.sh schedule 30m                   # (optional) auto re-ingest on a cron schedule
./docmcp.sh console                        # launch the admin/setup web UI
./docmcp.sh help                           # all commands
```

`setup` builds the slim **server** image right away; the heavier **ingest** image (Docling +
tree-sitter) is built the first time you run `./docmcp.sh ingest`. Under the hood the helper wraps
`docker compose -f docker/docker-compose.yml` (`run --rm ingest`, `up -d docs-mcp caddy`, …) — use
those directly for advanced control.

### Document corpus (`raw/`, version-controlled via Git LFS)

The `raw/` source corpus is tracked in git so nothing is silently overwritten/lost. Binary formats
(PDF/Office/images/archives) go through **Git LFS**; Markdown/text stay as normal diffable git (see
`.gitattributes`). One-time per machine: `git lfs install --local`. To keep a git-tracked copy,
add docs with `./docmcp.sh add /path/to/docs`, then `git add raw/ && git commit` — but note `add`
**copies** (duplicating storage); for a folder you don't need versioned in this repo, prefer
[`link`](#linking-external-folders-ingest-in-place--no-copy) (no copy). Pushing LFS objects needs
an LFS-capable remote. `.env`, `tokens.json`, and the built store (`var/`) stay ignored.

### Linking external folders (ingest in place — no copy)

`add` **copies** files into `raw/` (`cp -R`), so the corpus is self-contained and version-controlled
— but for a large corpus that doubles the storage. To ingest a folder **without copying it**, register
it as a *linked source*:

```bash
./docmcp.sh link /path/to/big/docs [name]   # register an external folder (read in place)
./docmcp.sh link --list                      # list linked sources
./docmcp.sh link --remove <name>             # unlink (re-ingest to drop its docs from the store)
./docmcp.sh ingest                           # bind-mounts each linked target read-only; no copy
```

`link` creates a registry symlink `raw/<name>` → your folder; on each `ingest`, the helper
bind-mounts the **real target** into the container at `/srv/linked/<name>:ro` and adds it to
`SOURCE_DIRS`, so the files are read from their original location (curated docs land under
`/<name>/…`). Notes:

- The folder must stay at the same host path at ingest time — a moved/deleted target is warned and
  skipped (not silently dropped).
- The target can't be the repo root or under `raw/` (it's already an ingest source — refused).
- A bare **symlink in `raw/` won't work** on its own: the ingest container only mounts `raw/`, the
  walker skips symlinks (`pipeline.py`), and the resolve-and-contain guard (`docstore.py`) rejects
  any path that resolves outside the root. `link` is what wires up the matching bind-mount so the
  data is actually reachable and contained. Edits in a linked folder are picked up on the next ingest.

---

## Deploy to a Linux server (x86_64)

Clone the repo on the target host **with Git LFS** (the models are LFS data — a pointer-only clone
breaks ingest), then run the same helper:

```bash
./docmcp.sh setup && ./docmcp.sh ingest && ./docmcp.sh serve
```

Two images come from one `docker/Dockerfile`: a slim **`server`** (FastMCP + ripgrep — the only
thing exposed, via Caddy) and a heavier **`ingest`** (Docling + tree-sitter [+ vector], run on
demand, never exposed). The curated store + index live in a named volume shared between them, so
`docs-mcp` binds `0.0.0.0:8080` **inside** the container but is **not published** — only Caddy is
reachable.

### Network-exposure profiles (pick one in `.env`)

- **Internal network over VPN (default, simplest):** reach the server by its **raw IP** over plain
  HTTP — nothing to install on client laptops. Set `HTTP_BIND=0.0.0.0` and
  `ALLOW_PLAINTEXT_HTTP=true`, add the server's IP to `ALLOWED_HOSTS`, point clients at
  `http://<server-ip>/mcp`. Custom port: `HTTP_PORT=8080` → `http://<server-ip>:8080/mcp` (use
  `HTTP_PORT`, **not** `BIND_PORT` or `DOMAIN=:port`). Bearer tokens are **not** encrypted on the
  wire — use this **only on a trusted private network** you control.
- **Public / untrusted network (HTTPS):** set `DOMAIN=docs-mcp.company.internal` (Caddy serves
  automatic HTTPS on 443; `:80` redirects) **and** `HTTP_BIND=0.0.0.0`. Needs the hostname
  resolvable/reachable for ACME (or a DNS-01 setup).
- **Local only:** comment both out — `HTTP_BIND` defaults to loopback and Caddy serves plain HTTP.

`./docmcp.sh serve` refuses to publish plaintext off loopback unless you set a `DOMAIN` (HTTPS) or
explicitly opt in with `ALLOW_PLAINTEXT_HTTP=true` — so cleartext on the network is always a
conscious choice, never a default accident.

### Port model

Clients only ever talk to **Caddy** on the one published port. The app's `8080` lives **inside** the
container — so to change the port users connect to, set `HTTP_PORT`, **not** `BIND_PORT`.

```
  CLIENT (Codex laptop)
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

### Cross-architecture builds

Building on an arm64 box (e.g. Apple Silicon) for an amd64 target? Build the images explicitly:

```bash
cd docker
docker buildx build --platform linux/amd64 --target server -t docs-mcp:server -f Dockerfile ..
docker buildx build --platform linux/amd64 --target ingest -t docs-mcp:ingest -f Dockerfile ..
```

### Scheduled ingest (cron)

Re-run ingestion automatically (it's incremental — unchanged files are skipped):

```bash
./docmcp.sh schedule 30m      # every 30 min  (or: hourly | daily | weekly | "m h dom mon dow")
./docmcp.sh schedule          # show the current schedule
./docmcp.sh schedule off      # remove it
```

It bakes in the right `docker` PATH and logs to `var/cron-ingest.log`. The job only fires while
Docker is running (on a server `dockerd` is always up; on a Mac, Docker Desktop must be open).

---

## Optional vector (semantic) search — fully offline

`semantic_search` lets an agent retrieve by *meaning*, not just keywords. It is **off by default**
and, when on, runs **entirely on-prem with no external API**: embeddings come from a vendored ONNX
model (`models/bge-small-en-v1.5`, English, 384-dim) run in-process via onnxruntime, and Qdrant is a
local container.

```bash
# in .env:  ENABLE_VECTOR=true        (EMBED_BACKEND=local is the default — no API key)
./docmcp.sh build server-vector       # serving image WITH the offline embedder + qdrant-client
./docmcp.sh ingest --full             # embeds chunks into Qdrant (offline)
./docmcp.sh serve                     # runs docs-mcp from the server-vector image + starts qdrant
```

Query embedding happens **inside the server process** (which is why vector serving uses the
`server-vector` image — the slim default has no embedder). Switching the embedding backend/model/dim
requires a full re-ingest (the Qdrant collection is rebuilt at the new vector size). When
`ENABLE_VECTOR=false` (default), `semantic_search` returns a clear disabled error and neither Qdrant
nor any embedder is contacted. `EMBED_BACKEND=openai` is a legacy **online** alternative (opt-in:
install `.[vector,vector-openai]`, set `OPENAI_API_KEY`) — not installed in the offline images.

---

## Issuing tokens

`tokens.json` maps an opaque token to a user and the path prefixes they may read; a token may also
reference **groups** (named prefix sets defined in `groups.json`):

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

A token's effective scope is **its explicit read prefixes + every prefix of every group it names**,
plus an optional **`--write`** (portal upload) scope — all combinable on one token and reloaded live.

**1. Mint a token from a group ("group token").** Define the group once, then issue tokens that
inherit its prefixes — edit the group later and every token that names it follows automatically:

```bash
./docmcp.sh group firmware /team-fw /team-fw-shared    # define/extend the group (read prefixes)
./docmcp.sh token bob --group firmware                 # bob inherits /team-fw + /team-fw-shared
./docmcp.sh token bob --group firmware --group public  # several groups → union of their prefixes
./docmcp.sh token bob /public --group firmware         # mix explicit prefixes with a group
```

**2. Grant write (upload) access.** `--write <prefix>` adds an upload scope on top of read scope;
repeat for several folders. Uploads land in `raw/` only (ingested by the `schedule`); `docs-mcp`
stays read-only. The write scope is usable only once the portal is enabled:

```bash
./docmcp.sh token alice /team-fw --write /team-fw            # read + write /team-fw
./docmcp.sh token alice --group firmware --write /team-fw    # group read + write
./docmcp.sh token alice /docs --write /docs --write /drafts  # write to two folders
```

**3. Add a user to a group.** Membership is expressed on the user's *token* — a member is anyone
holding a token that names the group. `token-rotate` keeps the *same* scope (so it can't add a
group); instead revoke the old token, then mint a fresh one that names the group:

```bash
./docmcp.sh token-rm carol                        # revoke carol's old token(s) first
./docmcp.sh token carol /public --group firmware  # mint fresh: /public + 'firmware' membership
./docmcp.sh access-tree                           # verify: groups → folders → members
./docmcp.sh access-check carol /team-fw/x.md      # → ALLOW  (firmware ⇒ /team-fw)
```

Prefer editing `tokens.json` directly? Add the name to that token's `"groups": [ … ]` array and
save the file **atomically** (temp + `mv`); the server reloads on its mtime change.

Keep `tokens.json` out of VCS and readable only by the server (it is bind-mounted read-only). The
server **reloads it automatically** when the file's mtime changes, so mint/revoke take effect on the
next request without a restart. An optional `"expires_at": <epoch>` per token is honored. Tokens are
compared in constant time and never logged.

### Optional: upload/manage portal

A browser portal lets non-technical teammates **publish docs without git**. It is a separate service
that writes **only** to `raw/` (the cron `schedule` ingests it); `docs-mcp` stays read-only. Grant
write access with `--write`, enable it, and start:

```bash
./docmcp.sh token alice /team-fw --write /team-fw   # read + write /team-fw
# .env: PORTAL_ENABLED=true, ALLOW_PLAINTEXT_PORTAL=true (VPN) or DOMAIN=<host> (HTTPS)
./docmcp.sh schedule 5m && ./docmcp.sh serve        # portal at <server>/portal
```

Full security model + operations in **[PORTAL.md](PORTAL.md)**.

---

## Connect a Codex client

Point the [OpenAI Codex](https://developers.openai.com/codex) CLI or IDE extension at the running
server with Codex's native **Streamable HTTP MCP** support. Each user needs: the MCP URL (ending in
`/mcp`), a bearer token scoped to their prefixes, and one `[mcp_servers.docs]` entry in
`~/.codex/config.toml` (or a trusted project `.codex/config.toml`). **Keep the token in an
environment variable — do not paste bearer tokens into `config.toml`.**

Common URLs:

| Use case | URL |
|---|---|
| Same machine as the server | `http://localhost/mcp` |
| Trusted VPN/LAN by raw IP | `http://10.0.0.5/mcp` |
| …with a custom server port | `http://10.0.0.5:8080/mcp` |
| Production / public network | `https://docs-mcp.company.internal/mcp` |

The URL's port follows the server's `HTTP_PORT`/`HTTPS_PORT`: the defaults (80/443) need no `:port`.
For non-local URLs, ensure the server's `ALLOWED_HOSTS` includes the exact hostname/IP Codex uses —
**host/IP only, no port**.

```bash
# 1. Operator: mint a scoped token, send it through a secure channel
./docmcp.sh token alice /public /team-fw --expires 90d

# 2. User: export the token in the shell that starts Codex
export DOCS_MCP_TOKEN=tok_alice_xxxx

# 3. User: register the MCP server
codex mcp add docs --url http://10.0.0.5/mcp --bearer-token-env-var DOCS_MCP_TOKEN

# 4. User: start Codex and verify; inside Codex run /mcp — the `docs` server should list its tools
codex
```

Hand-edit alternative (`clients/codex-config.example.toml`):

```toml
[mcp_servers.docs]
url = "http://10.0.0.5/mcp"
bearer_token_env_var = "DOCS_MCP_TOKEN"
startup_timeout_sec = 20
```

Management commands: `codex mcp list` · `codex mcp get docs` · `codex mcp remove docs`.

### Install the doc skills (optional)

The repo ships Codex [Agent Skills](https://developers.openai.com/codex/skills) under
`clients/skills/`:

| Skill | What it does |
|---|---|
| `docs` | find & cite the right docs (search → read → cite) |
| `doc-find` | locate a doc/section from a fuzzy, half-remembered description |
| `doc-report` | terminal inventory/overview of the docs |
| `doc-html-report` | export authorized docs into a self-contained HTML report |

Codex discovers skills as folders under `.agents/skills/`:

```bash
mkdir -p ~/.agents/skills                  # global (all projects)
cp -R clients/skills/* ~/.agents/skills/   # all of them …
# … or just one:     cp -R clients/skills/doc-html-report ~/.agents/skills/
# … or repo-scoped:  mkdir -p .agents/skills && cp -R clients/skills/* .agents/skills/
```

Restart Codex, then use them via `/skills`, `$doc-report` / `$doc-html-report`, or just by asking.
Writing docs that retrieve well is its own skill — see **[AUTHORING.md](AUTHORING.md)**.

### Troubleshooting

- **401 Unauthorized** → verify `DOCS_MCP_TOKEN` is exported in the shell that starts Codex and that
  the token exists in `tokens.json` (the server reloads on mtime change — no restart needed).
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
  Use your server's VPN/LAN IP; `--allow-http` is required for plain `http://` (remove it for `https://`).

---

## Develop without Docker (optional)

To hack on the code itself, run it on the host with [uv](https://docs.astral.sh/uv/) (Python 3.11+):

```bash
uv venv --python 3.11
uv pip install -e ".[dev,parse]"      # add ".[vector]" for semantic search
# ripgrep is the keyword backend: apt-get install ripgrep | brew install ripgrep
cp .env.example .env                  # for a host run set DOC_ROOT/SOURCE_DIRS to local paths
cp tokens.json.example tokens.json

uv run python -m docmcp.config        # print resolved settings
uv run docmcp-ingest --full --source ./raw   # build the doc store
uv run docmcp-server                  # serve on $BIND_HOST:$BIND_PORT/mcp
uv run pytest tests/unit              # fast test suite (no torch/Qdrant)
```

---

## Configuration

Server settings (see `.env.example`): `DOC_ROOT`, `DOCSTORE_ROOT`, `SOURCE_DIRS`,
`BIND_HOST`/`BIND_PORT`, `TOKENS_FILE`, `SEARCH_BACKEND` (`ripgrep`|`fts5`), `FTS5_DB`,
`ENABLE_VECTOR`, `QDRANT_URL`, `OPENAI_API_KEY`, `OPENAI_EMBED_MODEL`, `EMBED_CHUNK_TOKENS`,
`ALLOWED_ORIGINS`, `ALLOWED_HOSTS`, `TOKEN_TTL`, `LOG_REQUESTS`.

- **`DOCSTORE_ROOT`** (default = `DOC_ROOT`'s parent, e.g. `/srv/docs`): where the index, manifest,
  `ingest-status.json`, lock, and FTS5 db live — **outside** `DOC_ROOT` so they're never
  `read_doc`-able. `DOC_ROOT` must be a strict subdirectory of it.
- **`LOG_REQUESTS`** (default `true`): emit one structured JSON access-log line per tool call (user,
  tool, prefix count, path, result size, ms — never the token/content). Set `false` to disable.

Network exposure (consumed by `docmcp.sh` / Caddy — see the profiles above): `HTTP_BIND`,
`HTTP_PORT`/`HTTPS_PORT` (default 80/443), `DOMAIN` (a hostname switches Caddy to automatic HTTPS),
and `ALLOW_PLAINTEXT_HTTP`. Resource bounds (DoS guards): `MAX_SEARCH_LIMIT`, `MAX_READ_BYTES`,
`MAX_READ_LINES`.

> **Docker vs. bare run:** `BIND_HOST`/`BIND_PORT` set the in-container app listener and are
> **pinned** by `docker-compose.yml` on the Docker path (editing them in `.env` only affects a bare
> `uv run docmcp-server`). To change the **client-facing** port on Docker, set `HTTP_PORT` — not
> `BIND_PORT`. Host-side port/bind interpolation (`${HTTP_BIND}`/`${HTTP_PORT}`/`${DOMAIN}` in
> Caddy's `ports:`) is read only from `docker/.env`, and the plaintext-exposure guard lives only in
> `docmcp.sh serve`. So drive the stack through `./docmcp.sh`; a bare `docker compose up` skips both
> the port vars and that safety check.

---

## Testing

Tests are split into `tests/unit/` (fast — no torch/Qdrant), `tests/integration/` (live server,
Docling, Qdrant), and shell tests in `tests/shell/`.

```bash
uv run pytest tests/unit          # fast default — unit suite, no torch/Qdrant
uv run pytest                     # full suite (unit + integration)
uv run pytest -m "not docling and not vector"   # everything except Docling/torch + Qdrant
bash tests/shell/test_deploy_env.sh             # deploy-wizard .env helpers (pure bash)
```

Vector tests auto-skip unless a Qdrant is reachable on `localhost:6333`
(`docker run -p 6333:6333 qdrant/qdrant`).

---

## Security model

- **Path traversal is contained** in `docstore.py` (resolve-and-contain; the only filesystem
  resolver). Every tool intersects paths with the caller's `allowed_prefixes`.
- **Transport** validates the `Origin` header (DNS-rebinding) and an optional `ALLOWED_HOSTS`
  allowlist. The server only speaks plain HTTP and is never published directly — it is reached
  through Caddy, and you match the network profile to your trust boundary: plain HTTP by raw IP on a
  trusted/VPN network (gated by the explicit `ALLOW_PLAINTEXT_HTTP` opt-in), or automatic HTTPS via
  `DOMAIN` for an untrusted/public one. Bearer tokens must never travel plaintext on a network you
  don't trust.
- **Tokens** are constant-time compared, never logged, and honor an optional `expires_at`.
- **Resource bounds** (`MAX_READ_*`, `MAX_SEARCH_LIMIT`) keep an authenticated caller from
  exhausting resources. Add per-token rate limiting at the proxy (see `docker/Caddyfile`).
