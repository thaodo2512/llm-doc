#!/usr/bin/env bash
#
# docmcp.sh — Docker-based helper for the Documentation MCP Server.
#
# The ONLY thing you need installed is Docker (with the Compose plugin).
# No Python, uv, or ripgrep on the host — everything runs in containers.
# Linux/macOS compatible. Run `./docmcp.sh help` for usage.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

COMPOSE_FILE="$ROOT/docker/docker-compose.yml"
PROJECT="docs-mcp"                 # compose `name:` — prefixes the network/volumes
SERVER_IMAGE="docs-mcp:server"
NET="${PROJECT}_default"           # compose default network
DOCSTORE_VOL="${PROJECT}_docstore" # named volume holding the curated store + index

# --- pretty output ----------------------------------------------------------
if [ -t 1 ]; then C_B=$'\033[1m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_0=$'\033[0m'
else C_B=; C_G=; C_Y=; C_R=; C_0=; fi
info() { printf "%s\n" "${C_G}==>${C_0} $*"; }
warn() { printf "%s\n" "${C_Y}warning:${C_0} $*" >&2; }
die()  { printf "%s\n" "${C_R}error:${C_0} $*" >&2; exit 1; }

# --- helpers ----------------------------------------------------------------
# Force the project name (-p) so the network/volume names are deterministic even
# if the user has COMPOSE_PROJECT_NAME set in their environment.
dc() { docker compose -p "$PROJECT" -f "$COMPOSE_FILE" "$@"; }

need_docker() {
  command -v docker >/dev/null 2>&1 \
    || die "Docker is not installed. Install Docker Desktop (or Docker Engine): https://docs.docker.com/get-docker/"
  docker info >/dev/null 2>&1 \
    || die "Docker is installed but the daemon isn't running — start Docker Desktop / the docker service, then retry."
  docker compose version >/dev/null 2>&1 \
    || die "The Docker Compose plugin is missing. Install Compose v2+ (it ships with Docker Desktop)."
}

# Load .env (simple KEY=VALUE lines) and export so `docker compose` variable
# substitution (e.g. ${DOMAIN}) and our own logic can see it.
load_env() { if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi; }

is_true() { case "${1:-}" in true|TRUE|True|1|yes|on) return 0;; *) return 1;; esac; }

# Use `image ls -q` (not `image inspect <name>`): under Docker Desktop's containerd
# image store, `inspect` by short name:tag can spuriously report "No such image"
# even when the image exists and runs fine.
server_image_exists() { [ -n "$(docker image ls -q "$SERVER_IMAGE" 2>/dev/null)" ]; }

is_running() {
  local id; id="$(dc ps -q "$1" 2>/dev/null)" || return 1
  [ -n "$id" ] || return 1
  [ "$(docker inspect -f '{{.State.Running}}' "$id" 2>/dev/null)" = "true" ]
}

# Block until qdrant accepts connections (vector mode) so ingest doesn't race it.
wait_for_qdrant() {
  server_image_exists || return 0
  info "waiting for qdrant to be ready…"
  docker run --rm --network "$NET" "$SERVER_IMAGE" python - <<'PY' || die "qdrant did not become ready in time"
import socket, sys, time
for _ in range(30):
    try:
        socket.create_connection(("qdrant", 6333), 2).close(); sys.exit(0)
    except OSError:
        time.sleep(1)
sys.exit(1)
PY
}

# The URL a user points their MCP client at (served by caddy).
public_url() {
  local d="${DOMAIN:-:80}"
  case "$d" in
    ""|:80)  printf "http://localhost/mcp" ;;
    :*)      printf "http://localhost%s/mcp" "$d" ;;   # custom port, e.g. :8081
    *)       printf "https://%s/mcp" "$d" ;;           # real hostname -> Caddy TLS
  esac
}

# --- commands ---------------------------------------------------------------

cmd_setup() {
  need_docker
  info "Preparing configuration"
  [ -f "$ROOT/.env" ] || { cp "$ROOT/.env.example" "$ROOT/.env"; info "created .env (defaults are fine for a local run)"; }
  mkdir -p "$ROOT/raw"; [ -e "$ROOT/raw/.gitkeep" ] || : > "$ROOT/raw/.gitkeep"
  load_env

  info "Building the server image (one-time; usually 1–3 minutes)…"
  dc build docs-mcp

  if [ ! -f "$ROOT/tokens.json" ]; then
    printf '{}\n' > "$ROOT/tokens.json"
    local tok
    tok="$(cmd_token admin /)" || die "failed to create the admin token (see the Docker error above)"
    [ -n "$tok" ] || die "admin-token creation produced no token — check that 'docker run' works"
    info "created tokens.json with an 'admin' token (full access):"
    printf "    %s%s%s\n" "$C_B" "$tok" "$C_0"
  fi

  info "Setup complete. Next steps:"
  cat <<EOF
    ./docmcp.sh add /path/to/your/docs   # stage documents into raw/
    ./docmcp.sh ingest                   # build the searchable store
                                         #   (first run builds the ingestion image — large, several minutes)
    ./docmcp.sh serve                    # start the server (+ reverse proxy)
    ./docmcp.sh test                     # verify it answers
EOF
}

# add <file-or-dir>...  — stage documents into raw/ (plain file copy; no toolchain).
cmd_add() {
  [ "$#" -ge 1 ] || die "usage: ./docmcp.sh add <file-or-dir> [<file-or-dir> ...]"
  mkdir -p "$ROOT/raw"
  for src in "$@"; do
    [ -e "$src" ] || { warn "skip (not found): $src"; continue; }
    cp -R "$src" "$ROOT/raw/"
    info "added $src -> raw/"
  done
  info "Now run: ./docmcp.sh ingest"
}

# ingest [--full]  — (re)build the curated store from raw/ in the ingestion container.
cmd_ingest() {
  need_docker; load_env
  local profiles=(--profile ingest)
  if is_true "${ENABLE_VECTOR:-false}"; then
    profiles+=(--profile vector)
    info "vector search enabled — starting qdrant"
    dc --profile vector up -d qdrant
    wait_for_qdrant
  fi
  info "Ingesting raw/ → curated store"
  warn "the first ingest builds the ingestion image (installs Docling/torch wheels) — several minutes; the models are vendored in the repo, so none are downloaded"
  dc "${profiles[@]}" run --rm ingest "$@"
  if is_running docs-mcp; then
    dc restart docs-mcp >/dev/null && info "reloaded the running server (new docs are live)"
  fi
}

# serve  — start the server and reverse proxy in the background.
cmd_serve() {
  need_docker; load_env
  server_image_exists || die "build the image first: ./docmcp.sh setup"
  is_true "${ENABLE_VECTOR:-false}" && dc --profile vector up -d qdrant
  info "Starting the server + reverse proxy…"
  dc up -d docs-mcp caddy
  info "Server is live at: ${C_B}$(public_url)${C_0}"
  info "  logs: ./docmcp.sh logs    •    stop: ./docmcp.sh stop    •    check: ./docmcp.sh test"
}

# stop  — stop and remove the containers (named volumes / your data are kept).
cmd_stop() {
  need_docker
  info "Stopping all services (your ingested store is preserved)"
  dc --profile ingest --profile vector down
}

# logs  — follow the server + proxy logs.
cmd_logs() { need_docker; dc logs -f --tail=100 docs-mcp caddy; }

# build [server|ingest|all]  — (re)build images after code changes.
cmd_build() {
  need_docker
  case "${1:-server}" in
    server) dc build docs-mcp ;;
    ingest) dc --profile ingest build ingest ;;
    all)    dc build docs-mcp && dc --profile ingest build ingest ;;
    *)      die "usage: ./docmcp.sh build [server|ingest|all]" ;;
  esac
}

# token <user> [<prefix> ...]  — mint a scoped bearer token (default prefix: /).
cmd_token() {
  [ "$#" -ge 1 ] || die "usage: ./docmcp.sh token <user> [<allowed-prefix> ...]   (default prefix: /)"
  need_docker
  server_image_exists || die "build the image first: ./docmcp.sh setup"
  [ -f "$ROOT/tokens.json" ] || printf '{}\n' > "$ROOT/tokens.json"
  [ -w "$ROOT/tokens.json" ] || die "tokens.json is not writable: $ROOT/tokens.json"
  local user="$1"; shift
  local tok
  # Run as the host user so the bind-mounted tokens.json stays host-owned (Linux),
  # and write via a context manager so the file is always flushed/closed cleanly.
  tok="$(docker run --rm -i --user "$(id -u):$(id -g)" \
    -v "$ROOT/tokens.json:/work/tokens.json" "$SERVER_IMAGE" \
    python - /work/tokens.json "$user" "$@" <<'PY'
import json, os, secrets, sys
path, user = sys.argv[1], sys.argv[2]
prefixes = sys.argv[3:] or ["/"]
with open(path) as fh:
    data = json.load(fh) if os.path.getsize(path) > 0 else {}
tok = "tok_%s_%s" % (user, secrets.token_hex(12))
data[tok] = {"user": user, "allowed_prefixes": prefixes}
with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
print(tok)
PY
)"
  printf '%s\n' "$tok"
  if is_running docs-mcp; then
    dc restart docs-mcp >/dev/null && info "reloaded the running server (token is active)"
  else
    warn "start/restart the server for the new token to take effect: ./docmcp.sh serve"
  fi
}

# token-list  — show configured tokens (the file is plain JSON).
cmd_token_list() {
  local tokfile="$ROOT/tokens.json"
  [ -f "$tokfile" ] || die "no tokens.json yet — run: ./docmcp.sh setup"
  cat "$tokfile"
}

# token-rm <token|user>  — revoke a token (exact token string) OR every token
# belonging to a user, then reload the server so the revocation is live.
cmd_token_rm() {
  [ "$#" -ge 1 ] || die "usage: ./docmcp.sh token-rm <token-or-user>"
  need_docker
  server_image_exists || die "build the image first: ./docmcp.sh setup"
  [ -f "$ROOT/tokens.json" ] || die "no tokens.json yet — run: ./docmcp.sh setup"
  [ -w "$ROOT/tokens.json" ] || die "tokens.json is not writable: $ROOT/tokens.json"
  local target="$1"
  local removed
  # Edit the bind-mounted tokens.json as the host user, via a context manager.
  removed="$(docker run --rm -i --user "$(id -u):$(id -g)" \
    -v "$ROOT/tokens.json:/work/tokens.json" "$SERVER_IMAGE" \
    python - /work/tokens.json "$target" <<'PY'
import json, os, sys
path, target = sys.argv[1], sys.argv[2]
with open(path) as fh:
    data = json.load(fh) if os.path.getsize(path) > 0 else {}
if target in data:                       # exact token string
    removed = [target]
else:                                    # otherwise treat it as a user name
    removed = [t for t, r in data.items() if isinstance(r, dict) and r.get("user") == target]
for t in removed:
    del data[t]
with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
print("\n".join(removed))
PY
)" || die "failed to update tokens.json"
  [ -n "$removed" ] || die "no token or user matching '$target' (see: ./docmcp.sh token-list)"
  info "revoked:"
  printf '%s\n' "$removed" | sed 's/^/  /'
  if is_running docs-mcp; then
    dc restart docs-mcp >/dev/null && info "reloaded the running server (revocation is live)"
  else
    warn "start/restart the server for the revocation to take effect: ./docmcp.sh serve"
  fi
}

# test [<token>]  — exercise the running server (list_docs + read_doc).
cmd_test() {
  need_docker; load_env
  server_image_exists || die "run ./docmcp.sh setup first"
  is_running docs-mcp || die "the server isn't running — start it: ./docmcp.sh serve"
  local token="${1:-}"
  if [ -z "$token" ]; then
    [ -f "$ROOT/tokens.json" ] || die "no token given and no tokens.json (run ./docmcp.sh setup)"
    token="$(grep -oE '"tok_[^"]+"' "$ROOT/tokens.json" | head -n1 | tr -d '"')" || token=''
    [ -n "$token" ] || die "no token found in tokens.json — pass one: ./docmcp.sh test <token>"
  fi
  # The server's TrustedHostMiddleware only accepts Host values in ALLOWED_HOSTS
  # (default localhost). In normal use Caddy forwards that Host; for this direct
  # smoke test we send it explicitly. Use the configured DOMAIN if it's a hostname.
  local thost="localhost"
  case "${DOMAIN:-}" in ""|:*) thost="localhost" ;; *) thost="${DOMAIN}" ;; esac
  info "Testing the running server over the compose network…"
  docker run --rm -i --network "$NET" "$SERVER_IMAGE" \
    python - "http://docs-mcp:8080/mcp" "$token" "$thost" <<'PY'
import asyncio, sys
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
url, token, host = sys.argv[1], sys.argv[2], sys.argv[3]
async def main():
    tr = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}", "Host": host})
    async with Client(tr) as c:
        print("  tools     :", sorted(t.name for t in await c.list_tools()))
        docs = (await c.call_tool("list_docs", {})).data
        print("  list_docs :", [d.path for d in docs][:20], f"({len(docs)} total)")
        if docs:
            doc = (await c.call_tool("read_doc", {"path": docs[0].path})).data
            print(f"  read_doc  : {docs[0].path} -> {doc.total_lines} lines")
    print("OK")
asyncio.run(main())
PY
}

# status  — show docker state, the URL, and how many docs are indexed.
cmd_status() {
  need_docker; load_env
  printf "%sDocumentation MCP Server%s\n" "$C_B" "$C_0"
  printf "  %-10s %s\n" "docker"  "$(docker version -f '{{.Server.Version}}' 2>/dev/null || echo 'daemon not running')"
  printf "  %-10s %s\n" "url"     "$(public_url)"
  printf "  %-10s %s\n" "vector"  "${ENABLE_VECTOR:-false}"
  printf "  %-10s %s\n" "server"  "$(is_running docs-mcp && echo running || echo stopped)"
  printf "  %-10s %s\n" "proxy"   "$(is_running caddy && echo running || echo stopped)"
  if docker volume inspect "$DOCSTORE_VOL" >/dev/null 2>&1 && server_image_exists; then
    local n
    n="$(docker run --rm -v "$DOCSTORE_VOL:/srv/docs:ro" "$SERVER_IMAGE" \
      python -c 'import json,os; p="/srv/docs/curated/index.json"; print(len(json.load(open(p))) if os.path.exists(p) else 0)' 2>/dev/null || echo '?')"
    printf "  %-10s %s\n" "indexed" "${n} docs"
  else
    printf "  %-10s %s\n" "indexed" "(not built yet — run ./docmcp.sh ingest)"
  fi
}

# schedule [<spec>|off]  — run `ingest` on a cron schedule. <spec> is one of:
#   30m | 2h                 every N minutes (1-59) or hours (1-23)
#   hourly | daily | weekly  presets
#   "*/15 * * * *"           a raw 5-field cron expression (quote it)
# No arg shows the current schedule; `off` removes it. Idempotent: re-running
# replaces our entry and leaves any other crontab lines untouched.
_cron_marker() { printf '# docmcp-ingest:%s' "$ROOT"; }

cmd_schedule() {
  command -v crontab >/dev/null 2>&1 || die "'crontab' isn't available on this system"
  local spec="${1:-}"
  case "$spec" in
    ""|status|show)  _cron_status; return ;;
    off|remove|stop) _cron_remove; return ;;
  esac
  local cron_expr n
  case "$spec" in
    hourly) cron_expr="0 * * * *" ;;
    daily)  cron_expr="0 2 * * *" ;;
    weekly) cron_expr="0 2 * * 0" ;;
    *m) n="${spec%m}"; { [ "$n" -ge 1 ] && [ "$n" -le 59 ]; } 2>/dev/null \
          || die "minutes must be 1-59: $spec"; cron_expr="*/$n * * * *" ;;
    *h) n="${spec%h}"; { [ "$n" -ge 1 ] && [ "$n" -le 23 ]; } 2>/dev/null \
          || die "hours must be 1-23: $spec"; cron_expr="0 */$n * * *" ;;
    *)  [ "$(printf '%s' "$spec" | awk '{print NF}')" = 5 ] \
          || die "usage: ./docmcp.sh schedule <Nm|Nh|hourly|daily|weekly|'m h dom mon dow'|off>"
        cron_expr="$spec" ;;
  esac
  _cron_install "$cron_expr"
}

_cron_install() {
  local cron_expr="$1" marker logf dockerdir line current kept
  marker="$(_cron_marker)"
  dockerdir="$(dirname "$(command -v docker)")"
  logf="$ROOT/var/cron-ingest.log"; mkdir -p "$ROOT/var"
  line="$cron_expr cd \"$ROOT\" && PATH=\"$dockerdir:/usr/local/bin:/usr/bin:/bin\" ./docmcp.sh ingest >> \"$logf\" 2>&1 $marker"
  current="$(crontab -l 2>/dev/null || true)"
  kept="$(printf '%s\n' "$current" | grep -vF "$marker" || true)"
  { [ -n "$kept" ] && printf '%s\n' "$kept" || true; printf '%s\n' "$line"; } | crontab -
  info "scheduled — '$cron_expr' runs ${C_B}./docmcp.sh ingest${C_0}"
  info "  log: $logf   •   show: ./docmcp.sh schedule   •   remove: ./docmcp.sh schedule off"
  warn "fires only while Docker is running (on a Mac, Docker Desktop must be open)"
}

_cron_remove() {
  local marker current kept
  marker="$(_cron_marker)"
  current="$(crontab -l 2>/dev/null || true)"
  printf '%s\n' "$current" | grep -qF "$marker" || { info "no docmcp schedule was set"; return 0; }
  kept="$(printf '%s\n' "$current" | grep -vF "$marker" || true)"
  if [ -n "$kept" ]; then printf '%s\n' "$kept" | crontab -; else crontab -r 2>/dev/null || true; fi
  info "schedule removed"
}

_cron_status() {
  local marker line
  marker="$(_cron_marker)"
  line="$(crontab -l 2>/dev/null | grep -F "$marker" || true)"
  if [ -n "$line" ]; then info "current schedule:"; printf '  %s\n' "$line"
  else info "no schedule set — e.g.: ./docmcp.sh schedule 30m   (or hourly | daily | 'm h dom mon dow')"; fi
}

usage() {
  cat <<EOF
${C_B}docmcp.sh${C_0} — Documentation MCP Server helper (Docker-based; only Docker is required)

  ${C_B}setup${C_0}                     build the image, create .env + tokens.json (admin token)
  ${C_B}add${C_0} <path>...             stage files/dirs into raw/
  ${C_B}ingest${C_0} [--full]           build the searchable store from raw/ (in a container)
  ${C_B}serve${C_0}                     start the server + reverse proxy (background)
  ${C_B}test${C_0} [token]              exercise the running server (list/read)
  ${C_B}status${C_0}                    show services, URL, and index summary
  ${C_B}token${C_0} <user> [prefix...]  mint a bearer token (default prefix: /)
  ${C_B}token-list${C_0}                show configured tokens
  ${C_B}token-rm${C_0} <token|user>     revoke a token (or all of a user's tokens)
  ${C_B}logs${C_0}                      follow the server + proxy logs
  ${C_B}stop${C_0}                      stop services (your ingested store is kept)
  ${C_B}build${C_0} [server|ingest|all] (re)build images after code changes
  ${C_B}schedule${C_0} <Nm|Nh|daily|off> run 'ingest' on a cron schedule (no arg shows it)

First run:
  1. Install Docker Desktop (or Docker Engine + Compose).
  2. ./docmcp.sh setup
  3. ./docmcp.sh add /path/to/your/docs
  4. ./docmcp.sh ingest
  5. ./docmcp.sh serve   &&   ./docmcp.sh test

Connect a client (e.g. OpenAI Codex) to the printed URL with a bearer token —
see clients/codex-config.example.toml.
EOF
}

# --- dispatch ---------------------------------------------------------------
cmd="${1:-help}"; shift || true
case "$cmd" in
  setup)              cmd_setup "$@" ;;
  add)                cmd_add "$@" ;;
  ingest)             cmd_ingest "$@" ;;
  serve|up)           cmd_serve "$@" ;;
  stop|down)          cmd_stop "$@" ;;
  logs)               cmd_logs "$@" ;;
  build)              cmd_build "$@" ;;
  schedule|cron)      cmd_schedule "$@" ;;
  token)              cmd_token "$@" ;;
  token-list|tokens)  cmd_token_list "$@" ;;
  token-rm|token-remove|revoke) cmd_token_rm "$@" ;;
  test)               cmd_test "$@" ;;
  status)             cmd_status "$@" ;;
  help|-h|--help)     usage ;;
  *)                  warn "unknown command: $cmd"; usage; exit 1 ;;
esac
