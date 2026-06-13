# docmcp console — the admin/setup web UI

The **console** is a browser UI for setting up and operating a docmcp server: a first-run
**setup wizard** plus an ongoing **admin console** (tokens, groups, access, ingest,
status/health, schedule, config, backup, client-connect). It is a near-complete browser
replacement for the `docmcp.sh` CLI and the `local_deploy.sh` / `remote_deploy.sh` wizards.

It is **separate from the upload portal** (`docs/PORTAL.md`): the portal is a
container-confined, per-user surface for *contributors* to upload docs into `raw/`; the
console is a single-operator, host-level surface for *running the server*.

## Running it

```sh
./docmcp.sh console            # build the UI + console image if needed, then launch + open browser
./docmcp.sh console --build    # force-rebuild the SPA and the console image
./docmcp.sh console --port 9000
./docmcp.sh console --no-open   # don't auto-open the browser (just print the URL)
```

Or just run `./docmcp.sh` with no arguments for an interactive menu and pick **1**
(console · deploy locally · deploy to a server · setup · ops); on a non-interactive shell it
prints the full command list instead.

**First run is token-free.** On a brand-new checkout — before any admin token exists — the
console starts in **bootstrap** mode and prints a one-time link
(`http://127.0.0.1:8765/?bootstrap=…`). It **auto-opens that link in your browser** (macOS
`open`, WSL → the Windows browser, Linux `xdg-open`), which signs you straight into the setup
wizard — **you don't paste any token**. The wizard mints the admin token at the end; bootstrap
access closes automatically the moment it does.

> If you open the *bare* URL (`http://127.0.0.1:8765`, no `?bootstrap=…`) on a fresh checkout,
> you'll land on a login screen asking for an admin token that doesn't exist yet — that's the
> wrong door. Use the `?bootstrap=…` link the command prints (or just let it open the browser
> for you). Over SSH, or with `--no-open`, copy the printed bootstrap URL into your browser.

After setup, the console drops back to the normal login screen and you sign in with the admin
(whole-corpus, `--all`) token.

### How it runs (and why)

The console is a uvicorn app that **runs as a container on the host** (`docs-mcp:console` =
the server image + the Docker CLI), with two bind mounts:

- the **Docker socket** (`/var/run/docker.sock`) — so it can drive `docker compose` and the
  `docmcp.sh` verbs to build / ingest / serve / stop / schedule;
- the **repo at the same absolute path** (`$ROOT:$ROOT`) — so `docmcp.sh` inside resolves
  the same `ROOT` and Compose's relative mounts (`../raw`, `../tokens.json`) resolve on the
  host daemon. (This path identity is load-bearing — a different in-container path would
  break sibling-container mounts.)

Because it can run Docker and edit `tokens.json`, the console is effectively
**root-equivalent on the host**. It is therefore:

- **published on loopback only** — `cmd_console` refuses any non-loopback `--bind`. To reach
  it from another machine, tunnel: `ssh -L 8765:127.0.0.1:8765 <host>`.
- **admin-gated** — only a whole-corpus (`--all`) token may log in; scoped tokens get 403.
- **CSRF-protected** — every mutation carries an `X-CSRF-Token` header matched against the
  signed session cookie (same crypto as the portal).
- **audited** — every console action is appended to `var/console-audit.jsonl`.

### The security perimeter

The console **never reimplements** token/group/.env writes — it shells out to the existing
`docmcp.sh` verbs, which own the atomic-write + `flock` + audit + container-reload logic.
Every shell-out is built in `src/docmcp/console/commands.py` as a validated **argv list**
(never a shell string), from a frozen action set, after strict per-argument validation. A
shell metacharacter in a user value is an inert literal; a value can never be misread as a
flag (e.g. a user named `--all` is rejected). Reads that don't need Docker (tokens, groups,
access, config) are served directly from the bind-mounted files; status/doctor/inventory
shell out to the read-only verbs.

## Platforms

Works on **macOS, Ubuntu, and WSL Ubuntu** — the only host requirement is Docker (with the
Compose plugin). Cross-platform specifics the console handles for you:

- **Docker endpoint** — honors `DOCKER_HOST`, so rootless Docker
  (`unix://$XDG_RUNTIME_DIR/docker.sock`, common on Ubuntu) and Docker Desktop's WSL backend
  work, not just the default `/var/run/docker.sock`.
- **Socket group** — the gid that owns the socket *inside* the container is probed at launch
  (Docker Desktop maps it to `0`; rootful Linux preserves the host `docker` gid through the
  bind mount), so the non-root console user can reach the daemon on every platform.
- **File ownership** — the console runs as your host uid/gid, so `tokens.json`/`.env` it writes
  stay owned by you on native Linux (not root).
- **npm / buildx** — the dockerized SPA build and in-container `docker build` get a writable
  `HOME`, which Linux requires when running as an arbitrary uid.
- **Line endings** — `.gitattributes` pins `*.sh` (and the Dockerfile/Caddyfile) to LF, so a
  repo cloned through Windows git still runs under bash in WSL.

The host helper script (`docmcp.sh`) is bash-3.2-safe (macOS' system bash) and uses only
portable coreutils (no `sed -i`, GNU-only `find`/`stat`/`date` flags, etc.).

## Architecture

```
console-ui/                  React + TypeScript + Vite + Tailwind SPA (built to dist/)
src/docmcp/console/
  app.py        Starlette app: /api/* first, then the SPA from CONSOLE_STATIC_DIR
  routes.py     handlers (auth, reads, mutations, lifecycle jobs, SSE)
  commands.py   the allowlist: validated argv builders (the security perimeter)
  runner.py     subprocess execution: run_sync (reads) + JobRunner (long ops, SSE)
  auth.py       admin-gate login, bootstrap token, signed cookies, CSRF guard
  reads.py      direct structured reads (tokens/groups/access/config, redacted)
  audit.py      var/console-audit.jsonl
```

Long operations (build / ingest / serve / wizard) return a job id; the SPA streams the log
over Server-Sent Events (with a polling fallback). Job state lives in the runner, so you can
navigate away and re-attach.

## Development

The SPA builds with no host Node — `./docmcp.sh console --build` runs `npm install &&
npm run build` inside a throwaway `node:20` container, emitting `console-ui/dist/`, which the
console serves over the bind mount. For live UI work: `cd console-ui && npm run dev` (Vite on
:5173 proxies `/api` to the console on :8765).

For published wheels (no repo bind mount), build `console-ui/dist/` in CI and include it in
the package; set `CONSOLE_STATIC_DIR` to its installed location.

## Tests

- `tests/test_console_commands.py` — the allowlist: injection rejected, exact argv.
- `tests/test_console_api.py` — auth required, admin gate, CSRF, bootstrap flow, redaction.
- `tests/test_console_runner.py` — job capture, polling cursor, single-lifecycle lock.
- `tests/test_console_static.py` — no `shell=True`/`os.system`, subprocess only in
  `runner.py`, the `cmd_console` loopback + bootstrap guards.
