# docmcp — your docs, inside your coding agent

![License](https://img.shields.io/badge/license-Apache--2.0-blue)
![Protocol](https://img.shields.io/badge/protocol-MCP-6b5bd6)
![Deploy](https://img.shields.io/badge/deploy-self--hosted-159c72)
![Runs](https://img.shields.io/badge/runs-fully%20offline-c97815)

**docmcp** is a self-hosted [MCP](https://modelcontextprotocol.io) server that gives coding
agents (OpenAI Codex and other MCP clients) **search-and-cite** access to your internal
documentation — over **Streamable HTTP**, with per-user **bearer-token auth** and **path-prefix
RBAC**. An offline pipeline turns mixed sources (PDFs, Office docs, HTML, Markdown, and source
code) into a curated, searchable Markdown store. Everything runs on **your own infrastructure** —
no documents and no queries ever leave your network.

> 📊 **Prefer a visual tour?** Open **[`README.html`](README.html)** — a single illustrated page
> with an SVG architecture diagram and a capabilities overview. (Right-click → *Open in browser*.)

---

## What you can do

- **Ask your agent in plain language.** "Find the firmware flashing runbook and summarize it."
  The agent searches your docs, reads the relevant file, and **cites the exact path and line**.
- **Ingest almost anything.** PDFs, Word/PowerPoint/Excel, HTML, Markdown, and source code are
  parsed by **Docling + tree-sitter** into a clean Markdown store — tables and scanned pages
  included (OCR built in).
- **Control who sees what.** Each user gets a bearer token scoped to **path prefixes** (`/public`,
  `/team-fw`, …). Reusable **groups** keep grants tidy. A reader can never read outside their scope.
- **Search two ways.** Fast **keyword/full-text** search (ripgrep or SQLite FTS5) by default, plus
  an optional **semantic/vector** layer (Qdrant + a vendored ONNX embedder) that is **fully offline**.
- **Run it entirely on-prem.** Models are vendored in the repo; ingest and serving need **no
  external API and no internet** — ideal for air-gapped or privacy-sensitive environments.
- **Operate from the browser.** A **setup wizard + admin console** gets you running with no token
  to paste; an optional **upload portal** lets non-technical teammates publish docs without git.

---

## How it works

```
  raw sources              ingest                  curated store            serve
  ───────────              ──────                  ─────────────            ─────
  PDF · Office             Docling                 Markdown docs            docs-mcp (FastMCP)
  HTML · MD   ───────►   + tree-sitter   ───────►  + search index   ──────► · keyword + vector
  source code             (offline)                (keyword /              · per-token RBAC
                                                    optional vector)               │
                                                                                   │  reverse proxy
   coding agent  ◄──── Streamable HTTP + bearer token ─────────── Caddy ◄──────────┘
   (Codex, …)          (HTTP on VPN  ·  HTTPS in public)        (the only exposed port)
```

Only **Caddy** is published to the network; `docs-mcp` listens inside the container and is reached
through the proxy. The ingest pipeline runs on demand and is never exposed.

---

## Setup — three steps

The **only** thing the host needs is **Docker** (with the Compose plugin). No Python, no `uv`, no
ripgrep — the `./docmcp.sh` helper and the web console run everything in containers.

### 1 · Get the code (with Git LFS)

The repo ships the vendored offline models under `models/` as **Git LFS** data. You must pull the
real objects, not the pointer stubs, or ingestion fails.

```bash
# Install git-lfs ONCE per machine, BEFORE cloning:
#   apt-get install -y git-lfs  |  dnf install git-lfs  |  brew install git-lfs
git lfs install

git clone <repo-url> && cd <repo>     # LFS objects download automatically
./docmcp.sh models                    # verify the clone is complete (--repair if not)
```

> Only touching docs, the frontend, or Python/bash logic? Skip the ~530 MB of models with
> `GIT_LFS_SKIP_SMUDGE=1 git clone <repo-url>` and pull them later only if you build the ingest
> image. See **[docs/OPERATING.md](docs/OPERATING.md#models--git-lfs)** for the full LFS guide.

### 2 · Launch the server

**Easiest — the browser console (recommended):**

```bash
./docmcp.sh console --docs /path/to/your/docs
```

This opens a **setup wizard** in your browser (no token to paste on first run), indexes the docs
folder you point it at, mints your admin token, and starts the server. From then on the same
console manages tokens, ingest, schedules, health, and client connect.

**Or the scriptable CLI:**

```bash
./docmcp.sh setup                  # build the image; create .env + admin token
./docmcp.sh link /path/to/docs     # register your docs folder (read in place — no copy)
./docmcp.sh ingest                 # build the searchable store
./docmcp.sh serve                  # start the server + reverse proxy
```

The server is then reachable at **`http://<server-ip>/mcp`**. The shipped default is the
**internal-network profile**: plain HTTP over a trusted network (e.g. VPN). For a public network,
set `DOMAIN=docs.company.internal` and Caddy serves **automatic HTTPS**. Full network-exposure
profiles, ports, and the security model live in **[docs/OPERATING.md](docs/OPERATING.md)**.

### 3 · Connect your coding agent

Mint a scoped token and point [Codex](https://developers.openai.com/codex) at the server:

```bash
# operator: mint a token scoped to the prefixes this user may read
./docmcp.sh token alice /public /team-fw --expires 90d

# user: register the MCP server (keep the token in an env var, never in config files)
export DOCS_MCP_TOKEN=tok_alice_xxxx
codex mcp add docs --url http://10.0.0.5/mcp --bearer-token-env-var DOCS_MCP_TOKEN
```

Use `http://localhost/mcp` on the same machine, or `https://docs.company.internal/mcp` in
production. Start `codex`, run `/mcp`, and the `docs` server should list its tools:
`list_docs`, `search_docs`, `read_doc`, `semantic_search`. The full client guide (config.toml,
the `mcp-remote` fallback, and troubleshooting) is in
**[docs/OPERATING.md](docs/OPERATING.md#connect-a-codex-client)**.

### Adding more documents (anytime)

You don't take the server down to grow the corpus. The recommended way to add a folder is to
**link** it — ingest reads it in place, with no second copy — then rebuild the index:

```bash
./docmcp.sh link /path/to/more/docs   # register the folder (read in place — no copy)
./docmcp.sh ingest                     # rebuild the index (incremental — unchanged files are skipped)
```

Re-ingest is **incremental**, so it's cheap to run as often as you like. Other ways to add docs:

- **Copy into the corpus** — `./docmcp.sh add <path>` copies files into the version-controlled
  `raw/` folder. Fine for a single file or when you want a git-tracked copy, but it **duplicates
  storage** — for a folder, prefer `link` (it warns you and points you there).
- **From the browser** — the console's *Ingest* page has a one-click **Run ingest** (`./docmcp.sh console`).
- **On a schedule** — `./docmcp.sh schedule 30m` re-ingests automatically (pairs well with the portal).
- **Without git** — let non-technical teammates upload through the optional
  [portal](docs/PORTAL.md); the schedule picks their files up on the next tick.

### Keeping it running across reboots

The serving stack is set to restart itself, and your data lives in Docker volumes plus repo
files — so a reboot of the machine loses nothing:

- **Your data persists.** The curated store, search index, TLS certs, and vector DB live in Docker
  named volumes; `tokens.json`, `groups.json`, and `raw/` are files in the repo. All survive a
  reboot. (Only `./docmcp.sh uninstall` deletes data — a normal `stop` never does.)
- **The server comes back on its own.** `docs-mcp`, Caddy, and (if enabled) Qdrant and the portal
  run with `restart: unless-stopped`, so Docker restarts them when the daemon starts — **no
  `./docmcp.sh serve` re-run needed**.
- **On a Mac laptop, the one catch is Docker Desktop.** It doesn't launch at login unless you turn
  that on (Docker Desktop → *Settings → General → Start Docker Desktop when you sign in*). Once
  Docker is up after a reboot the stack self-heals — confirm with `./docmcp.sh status`. On a Linux
  server `dockerd` is a system service, so it's fully automatic.
- The admin **console** is a foreground process and won't reappear by itself — re-run
  `./docmcp.sh console` only if you want the UI (it isn't needed for serving). A configured
  `schedule` survives the reboot and resumes once Docker is running.

---

## Using it day to day

Once connected, just ask — the agent searches, reads, and cites the doc path it used. Search is
keyword-first, so name the **exact terms** (commands, config keys, error strings, symbols):

```text
Find the firmware flashing runbook and summarize the steps.
Search our docs for DEPLOY_TOKEN and cite the doc path and line.
What does our PLDM spec say about completion codes? Cite the spec path.
```

The repo also ships Codex **[Agent Skills](https://developers.openai.com/codex/skills)** under
`clients/skills/` — `docs` (find & cite), `doc-find` (locate a half-remembered doc),
`doc-report` (terminal inventory), and `doc-html-report` (shareable HTML export).

---

## The four tools

| Tool | Returns | Notes |
|------|---------|-------|
| `list_docs(path="")` | index entries under `path` | pre-filtered to your prefixes |
| `search_docs(query, limit=10)` | `{path, line, snippet, score}` | keyword (ripgrep / FTS5) |
| `read_doc(path, start_line?, end_line?)` | `{path, content, …}` | denies paths outside your prefixes |
| `semantic_search(query, limit=10)` | `{path, line, snippet, score}` | off unless `ENABLE_VECTOR=true` |

Every tool intersects paths with the caller's `allowed_prefixes`; `read_doc` **denies** (not
silently empties) a disallowed path.

---

## Documentation

| Guide | What's in it |
|-------|--------------|
| **[docs/OPERATING.md](docs/OPERATING.md)** | Full operator reference — deploy profiles & ports, the complete `./docmcp.sh` command set, token/group recipes, vector search, scheduled ingest, configuration, testing, troubleshooting, security |
| **[docs/CONSOLE.md](docs/CONSOLE.md)** | The admin/setup web console (setup wizard + ongoing operations) |
| **[docs/PORTAL.md](docs/PORTAL.md)** | The optional upload portal for non-technical contributors |
| **[docs/AUTHORING.md](docs/AUTHORING.md)** | Writing docs that retrieve well |
| **[README.html](README.html)** | Illustrated single-page overview (SVG architecture diagram) |

---

## License

Licensed under the Apache License, Version 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
Copyright 2026 The docmcp Authors.
