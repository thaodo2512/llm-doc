# Documentation MCP Server — Presenter Transcript

Speaker notes for `docmcp-project-slides.html` (14 slides). Each section is roughly
what to say while that slide is on screen. Timings are a guide for a ~12–15 minute
talk; trim the optional/tech-stack slides for a 7-minute version.

---

## Slide 1 — Title: Documentation MCP Server

Welcome. This is the **Documentation MCP Server** — a self-hosted bridge that lets a
coding agent like Codex search and read your internal documentation through the Model
Context Protocol.

The whole idea fits on this slide: the agent makes an MCP call, the server checks who's
asking and what they're allowed to see, and it answers out of a curated doc store. Three
verbs drive everything — **find** the right terms, **read** a bounded slice, and **cite**
the path and line so every answer is traceable.

Three properties to keep in mind as we go: it speaks **Streamable HTTP**, every request
carries a **bearer token**, and access is gated by **path-prefix RBAC**.

---

## Slide 2 — What is MCP?

A quick level-set on MCP for anyone new to it.

MCP gives the agent a small set of **named tools** with typed schemas, instead of asking
it to guess where documents live or to scrape a wiki. The agent turns a natural-language
question into a tool call; the server validates auth and RBAC *before* any content leaves;
and the answer comes back citing real document paths.

So there are two moments that matter here: **the agent asks** — language becomes a
structured call — and **the server gates** — nothing is returned until the caller is
authorized. The tools you see on the right (`list_docs`, `search_docs`, `read_doc`) are
the entire contract.

---

## Slide 3 — Problem

Why build this at all? The documentation usually *exists* — the hard part is finding the
right slice of it.

Three pain points. First, it's **scattered**: PDFs, source notes, runbooks, HTML, and
Markdown all live in different places. Second, it's **hard to search**: people remember a
config key, an error string, or a command — not the title of the page it's on. Third,
it's **access-sensitive**: search has to respect team boundaries *before* an answer is
generated, not after.

The picture at the bottom is the status quo — five different stores and a person asking
"where is it?" That's the question we're answering automatically.

---

## Slide 4 — Core idea

The core idea is to turn every source into **one curated, searchable store**, and to keep
the serving path read-only.

Raw documents go through an **ingest** pipeline — Docling for PDFs and Office files, OCR
for scans, tree-sitter for code, plain parsers for text. Out comes **curated Markdown** at
stable logical paths, plus a **keyword index** (ripgrep or SQLite FTS5; vector search is
optional). The agent only ever touches the tools at the end.

The one line to remember is at the bottom: `raw → ingest → curated + index → list / search
/ read`. Ingest is the only thing that writes; the server only reads.

---

## Slide 5 — Core tools

The tool surface is deliberately small — three tools, predictable results.

- `list_docs` — **discover**. Returns the visible docs under a prefix: path, title, type,
  size, mtime. You call this first.
- `search_docs` — **find exact terms**. Keyword and full-text hits, each with a path,
  line, snippet, and score. Best for code symbols, config keys, error strings.
- `read_doc` — **open the source**. Reads a bounded document or a specific line range, and
  denies anything outside your prefixes.

Two things hold across all of them: `semantic_search` is **optional** — vector search only
runs if you turn it on, the default is keyword-first — and **every tool is
prefix-filtered**, so a token sees only its allowed paths and denied reads fail closed.

---

## Slide 6 — Architecture

Here's how it's actually deployed. The key property: **Caddy is the only exposed door, and
reads and writes never share a path.**

Two clients on the left — Codex over MCP, and a teammate's browser. Both enter through
**Caddy**, which terminates TLS. Caddy routes `/mcp` to the read-only **docs-mcp** service
and `/portal` to the optional upload portal. Neither app publishes a port of its own — only
the proxy is reachable.

On the right is the data side. Both services read `tokens.json` and `groups.json` for auth
and RBAC, **read-only**. The portal writes uploads only into **`raw/` staging**. Then
**ingest** — on a cron or on demand — reads `raw/` read-only and is the *sole writer* of
the **curated docs + index**, which docs-mcp mounts read-only. Follow the mount labels:
`:ro` everywhere except the single `rw` arrow from the portal into staging. One writer, one
direction.

---

## Slide 7 — Security model

Security is layered — every request passes through several independent checks, so no single
mistake opens the door.

- **Authentication** — opaque bearer tokens, compared in constant time, with optional
  expiry. Revoke or rotate takes effect live, without a restart.
- **RBAC on every tool** — segment-aware prefix scoping, deny-by-default. `/pub` does not
  match `/public`, and a group can never grant the whole corpus.
- **Path containment** — a single resolver maps logical paths to real ones; `..`, absolute
  paths, and symlink escapes are rejected *before* the RBAC check runs.
- **Resource bounds** — read byte and line caps, a search-limit clamp, upload size and
  count caps. One authenticated caller can't exhaust the box.
- **Transport hardening** — TLS at Caddy, a Host allowlist, and Origin validation against
  DNS-rebinding. The app ports are never published.
- **Write isolation** — the server is read-only; the portal writes only `raw/`, behind CSRF
  and signed cookies, with an audit log.

The takeaway: even if one layer were bypassed, the next one still holds.

---

## Slide 8 — Local model

Deployment scales down to a single machine. On one laptop or workstation, Codex and the
server run side by side and talk over a **loopback URL**.

The config is three lines: `codex mcp add docs`, point it at `http://localhost/mcp`, and
hand it the token through an environment variable. Nothing leaves the box. This is the
fastest way to try it, and it's a perfectly good single-user setup.

---

## Slide 9 — Remote model

For a team, you run it as a shared server and pick the exposure that matches your trust
boundary.

On a **trusted VPN or LAN** you control, a raw IP over plain HTTP is simple — just point at
`http://10.0.0.5/mcp`. For a **public or untrusted network**, use a hostname and let Caddy
serve automatic **HTTPS**, so bearer tokens always travel over TLS.

Either way, the request carries an `Authorization: Bearer` token, and `ALLOWED_HOSTS` gates
which Host headers — or IPs — the server will answer to.

---

## Slide 10 — Deploy and setup

Day-to-day operation is one helper script: `docmcp.sh`.

The operator path is five commands: **setup** builds the images and mints an admin token,
**add** registers a source directory, **ingest** converts and indexes it, **serve** starts
docs-mcp and Caddy, and **token** mints a scoped token — here `alice` gets `/public` and
`/team-fw`.

On the client side, the user exports that token and runs `codex mcp add docs` against the
server URL. That's the whole onboarding. The three tiles summarize the lifecycle: **setup,
ingest, serve.**

---

## Slide 11 — Usage examples

In practice, nobody calls the tools by hand — they ask naturally and the agent chains the
tools for them.

Three examples: "Find the firmware flashing runbook and summarize the steps" — the agent
searches, reads, and summarizes. "Search our docs for `DEPLOY_TOKEN` and cite the path and
line" — exact-term lookup with a citation. "Open `/team-fw/flashing.md` lines 1–40" — a
direct bounded read.

Under the hood it's always the same chain: **ask → search → read → cite.** The agent picks
the tools; the user just asks the question.

---

## Slide 12 — Optional capabilities

The base system is read-only retrieval, but it grows when the team needs more.

- **Portal** — a browser upload-and-manage flow for teammates who don't use git.
- **Scheduled ingest** — rebuild incrementally on a cron interval; unchanged files are
  skipped, so reruns are cheap.
- **Groups and RBAC** — map a team to a set of prefixes once, and every token in that group
  inherits the access live.

The commands at the bottom show all three: `schedule 30m`, `group firmware /team-fw`, and
`access-check` to confirm what a given user can reach. Each of these is opt-in — you turn
them on only when you need them.

---

## Slide 13 — Tech stack

None of this is built from scratch — it stands on proven open source.

- **FastMCP** — the MCP server framework: tools, bearer auth, Streamable HTTP.
- **Caddy** — the reverse proxy with automatic HTTPS; the only exposed door.
- **Starlette + Uvicorn** — the ASGI framework and server behind both the MCP app and the
  portal.
- **Docker Compose** — service isolation, read-only mounts, internal-only ports.
- **Docling** — converts PDF, Office, and HTML to Markdown, including table structure.
- **RapidOCR** — OCR for scanned pages and embedded images at ingest time.
- **tree-sitter** — structure-aware parsing of source-code documents.
- **ripgrep** — the default keyword backend, fixed-string matching.
- **SQLite FTS5** — an alternative full-text index with ranked line hits.
- **Qdrant** — the optional vector store for semantic search.

Each line on the slide links to the upstream project if you want to dig in.

---

## Slide 14 — Closing

To wrap up: this gives a team **one path from a question to a cited answer.**

Three outcomes. **Less hunting** — search exact terms across the whole corpus instead of
clicking through wikis. **Source-backed** — every answer cites a doc path and line, so it's
verifiable. And **scoped** — every tool call respects RBAC, so the agent never surfaces
something the asker isn't allowed to see.

docs-mcp sits in the middle as a trusted retrieval layer between your people, your agents,
your raw documents, and your access rules. Happy to take questions.
