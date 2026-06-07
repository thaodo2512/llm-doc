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
# Pass --env-file so ${VAR} interpolation in the compose file resolves from the
# repo-root .env regardless of the caller's cwd (compose otherwise auto-discovers
# .env next to the compose file, i.e. docker/.env, not repo root).
dc() {
  local ef=(); [ -f "$ROOT/.env" ] && ef=(--env-file "$ROOT/.env")
  # ${ef[@]+...} keeps this safe under `set -u` with an empty array on bash 3.2 (macOS).
  docker compose -p "$PROJECT" ${ef[@]+"${ef[@]}"} -f "$COMPOSE_FILE" "$@"
}

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

# Fail fast if any vendored model is still a Git LFS pointer (a clone without
# `git lfs pull`). The ingest image bakes models/ at build time, so a pointer text
# file becomes a fake "model config" and Docling dies at ingest with a cryptic
# JSONDecodeError on PDFs with tables/OCR. Catch it here before the long build.
check_lfs_models() {
  [ -d "$ROOT/models" ] || return 0
  local ptrs; ptrs="$(grep -rIl 'git-lfs.github.com/spec' "$ROOT/models" 2>/dev/null)" || true
  if [ -n "$ptrs" ]; then
    printf '%s\n' "$ptrs" | sed "s,^$ROOT/,  ," >&2
    die "the vendored models above are un-materialized Git LFS pointers. Run 'git lfs install && git lfs pull', then rebuild — otherwise ingestion fails with a JSONDecodeError on PDFs with tables/OCR."
  fi
}

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
  local d="${DOMAIN:-}" port="${HTTP_PORT:-80}"
  case "$d" in
    ""|:*)   # plaintext HTTP (no hostname); Caddy publishes on $HTTP_PORT
             case "$port" in
               80) printf "http://localhost/mcp" ;;
               *)  printf "http://localhost:%s/mcp" "$port" ;;
             esac ;;
    *)       printf "https://%s/mcp" "$d" ;;           # real hostname -> Caddy TLS
  esac
}

# --- commands ---------------------------------------------------------------

cmd_setup() {
  need_docker
  info "Preparing configuration"
  [ -f "$ROOT/.env" ] || { cp "$ROOT/.env.example" "$ROOT/.env"; chmod 600 "$ROOT/.env"; info "created .env (mode 600). Default profile = internal network over VPN (plain HTTP by raw IP): set your server's IP in ALLOWED_HOSTS. See .env.example for the HTTPS or local-only profiles."; }
  mkdir -p "$ROOT/raw"; [ -e "$ROOT/raw/.gitkeep" ] || : > "$ROOT/raw/.gitkeep"
  load_env

  info "Building the server image (one-time; usually 1–3 minutes)…"
  dc build docs-mcp

  if [ ! -f "$ROOT/tokens.json" ]; then
    ( umask 077; printf '{}\n' > "$ROOT/tokens.json" ); chmod 600 "$ROOT/tokens.json"
    local tok
    # Admin is the break-glass token: full access, non-expiring. Mint scoped,
    # expiring tokens for everyone else.
    tok="$(cmd_token admin / --expires never)" || die "failed to create the admin token (see the Docker error above)"
    [ -n "$tok" ] || die "admin-token creation produced no token — check that 'docker run' works"
    info "created tokens.json (mode 600) with an 'admin' token — full access, non-expiring:"
    printf "    %s%s%s\n" "$C_B" "$tok" "$C_0"
    warn "keep the admin token secret (break-glass). For others, mint scoped expiring tokens, e.g.: ./docmcp.sh token alice /public --expires 90d"
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
  need_docker; load_env; check_lfs_models
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
  # DOMAIN=:<port> (anything but :80) is unsupported: Caddy would listen on that
  # container port, but compose only publishes HTTP_PORT->80 / HTTPS_PORT->443, so
  # the endpoint would be unreachable while the helper advertised it as live. To
  # change the published port use HTTP_PORT; for HTTPS use a hostname DOMAIN.
  case "${DOMAIN:-}" in
    ""|:80) ;;
    :*) die "DOMAIN=${DOMAIN} (a bare :port) is not supported — Caddy would listen on a container port that isn't published, so clients couldn't reach it. To change the client-facing port set HTTP_PORT=<port> in .env (leave DOMAIN unset); to serve HTTPS set DOMAIN=<hostname>." ;;
  esac
  # Network-exposure policy. Publishing the plaintext :80 listener off loopback
  # sends bearer tokens in cleartext, so it is gated (the original HIGH finding was
  # one env var away from cleartext on all interfaces):
  #   - DOMAIN=<hostname>          -> Caddy serves HTTPS; binding off loopback is fine.
  #   - ALLOW_PLAINTEXT_HTTP=true  -> conscious opt-in for a TRUSTED private network
  #                                   (e.g. reachable only over VPN). Tokens are NOT
  #                                   encrypted; never use on untrusted/public nets.
  #   - otherwise                  -> refuse.
  case "${HTTP_BIND:-127.0.0.1}" in
    127.0.0.1|localhost|::1) ;;
    *) case "${DOMAIN:-:80}" in
         ""|:*)
           if is_true "${ALLOW_PLAINTEXT_HTTP:-false}"; then
             warn "ALLOW_PLAINTEXT_HTTP=true — serving plaintext HTTP on ${HTTP_BIND} with NO TLS. Bearer tokens travel in cleartext; only safe on a trusted private network (e.g. reachable solely over VPN). Do NOT use on an untrusted or public network."
           else
             die "HTTP_BIND=${HTTP_BIND} would publish plaintext HTTP off loopback but there is no TLS (DOMAIN is not a hostname) — bearer tokens would travel in cleartext. Pick one: set DOMAIN=<host> for HTTPS; or, for a TRUSTED private network (VPN/internal), set ALLOW_PLAINTEXT_HTTP=true to accept plaintext; or keep HTTP_BIND on loopback for local-only."
           fi ;;
       esac ;;
  esac
  is_true "${ENABLE_VECTOR:-false}" && dc --profile vector up -d qdrant
  info "Starting the server + reverse proxy…"
  dc up -d docs-mcp caddy
  info "Server is live at: ${C_B}$(public_url)${C_0}"
  local portsfx=""; case "${HTTP_PORT:-80}" in 80|"") ;; *) portsfx=":${HTTP_PORT}";; esac
  case "${DOMAIN:-:80}" in
    ""|:80|:*)
      case "${HTTP_BIND:-127.0.0.1}" in
        127.0.0.1|localhost|::1) warn "no DOMAIN set: serving plain HTTP on loopback (127.0.0.1) only — local access. To reach it over your internal network/VPN by IP, set HTTP_BIND=0.0.0.0 + ALLOW_PLAINTEXT_HTTP=true (plaintext — trusted networks only); for an untrusted/public network set DOMAIN=<hostname> for HTTPS." ;;
        *) info "reachable over your internal network at ${C_B}http://<server-ip>${portsfx}/mcp${C_0} (plaintext — keep this on a trusted/VPN network; add <server-ip> to ALLOWED_HOSTS)." ;;
      esac ;;
  esac
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
    ingest) check_lfs_models; dc --profile ingest build ingest ;;
    all)    check_lfs_models; dc build docs-mcp && dc --profile ingest build ingest ;;
    *)      die "usage: ./docmcp.sh build [server|ingest|all]" ;;
  esac
}

# token <user> [<prefix> ...] [--expires <Nd|Nh|Nm|never>]  — mint a scoped bearer
# token. Default prefix: /. Default expiry: 90 days (override TOKEN_TTL or pass
# --expires; use 'never' for a non-expiring token).
cmd_token() {
  [ "$#" -ge 1 ] || die "usage: ./docmcp.sh token <user> [<allowed-prefix> ...] [--expires <Nd|Nh|Nm|never>]"
  need_docker
  server_image_exists || die "build the image first: ./docmcp.sh setup"
  [ -f "$ROOT/tokens.json" ] || { ( umask 077; printf '{}\n' > "$ROOT/tokens.json" ); }
  [ -w "$ROOT/tokens.json" ] || die "tokens.json is not writable: $ROOT/tokens.json"

  # Pull a --expires flag out of the args; whatever remains is user + prefixes.
  local expires_spec="${TOKEN_TTL:-90d}" rest=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --expires)   expires_spec="${2:-}"; shift 2 || die "--expires needs a value" ;;
      --expires=*) expires_spec="${1#--expires=}"; shift ;;
      *)           rest+=("$1"); shift ;;
    esac
  done
  set -- "${rest[@]}"
  local user="${1:-}"; [ -n "$user" ] && shift || die "usage: ./docmcp.sh token <user> [<prefix> ...] [--expires <Nd|Nh|Nm|never>]"

  # Translate the spec into a TTL in seconds (empty string => non-expiring).
  # Validate the numeric body BEFORE the arithmetic so a malformed spec can't crash
  # under `set -u` or smuggle in a negative / hex / overflowing value.
  local ttl="" n="" unit=""
  case "$expires_spec" in
    never|none|0|"") ttl="" ;;
    *d) unit=86400; n="${expires_spec%d}" ;;
    *h) unit=3600;  n="${expires_spec%h}" ;;
    *m) unit=60;    n="${expires_spec%m}" ;;
    *)  die "invalid --expires '$expires_spec' (use Nd | Nh | Nm | never)" ;;
  esac
  if [ -n "$unit" ]; then
    [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le 36500 ] \
      || die "invalid --expires '$expires_spec' (use a positive Nd | Nh | Nm up to 36500, or never)"
    ttl=$(( n * unit ))
  fi

  local tok
  # Run as the host user so the bind-mounted tokens.json stays host-owned (Linux),
  # and write via a context manager so the file is always flushed/closed cleanly.
  tok="$(docker run --rm -i --user "$(id -u):$(id -g)" \
    -v "$ROOT/tokens.json:/work/tokens.json" "$SERVER_IMAGE" \
    python - /work/tokens.json "$user" "$ttl" "$@" <<'PY'
import json, os, secrets, sys, time
path, user, ttl = sys.argv[1], sys.argv[2], sys.argv[3]
prefixes = sys.argv[4:] or ["/"]
with open(path) as fh:
    data = json.load(fh) if os.path.getsize(path) > 0 else {}
rec = {"user": user, "allowed_prefixes": prefixes}
if ttl:
    rec["expires_at"] = int(time.time()) + int(ttl)
tok = "tok_%s_%s" % (user, secrets.token_hex(12))
data[tok] = rec
with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
print(tok)
PY
)"
  chmod 600 "$ROOT/tokens.json" 2>/dev/null || true
  printf '%s\n' "$tok"
  # Notes go to stderr so a caller capturing `$(... token ...)` gets only the token.
  if [ -n "$ttl" ]; then info "expires in ${expires_spec}" >&2; else info "non-expiring token" >&2; fi
  if is_running docs-mcp; then
    dc restart docs-mcp >/dev/null && info "reloaded the running server (token is active)" >&2
  else
    warn "start/restart the server for the new token to take effect: ./docmcp.sh serve"
  fi
}

# token-list  — show configured tokens with the secret REDACTED (user, prefixes,
# expiry only). Never prints the full token string.
cmd_token_list() {
  local tokfile="$ROOT/tokens.json"
  [ -f "$tokfile" ] || die "no tokens.json yet — run: ./docmcp.sh setup"
  need_docker
  server_image_exists || die "build the image first: ./docmcp.sh setup"
  docker run --rm -i -v "$tokfile:/work/tokens.json:ro" "$SERVER_IMAGE" \
    python - /work/tokens.json <<'PY'
import json, os, sys, time
path = sys.argv[1]
data = json.load(open(path)) if os.path.getsize(path) > 0 else {}
if not data:
    print("(no tokens)"); raise SystemExit
now = time.time()
for tok, rec in data.items():
    shown = (tok[:8] + "…" + tok[-4:]) if len(tok) > 14 else "…"
    exp = rec.get("expires_at")
    if exp:
        status = "EXPIRED" if exp < now else "expires " + time.strftime("%Y-%m-%d", time.localtime(exp))
    else:
        status = "no expiry"
    print("  %-16s  user=%-12s  prefixes=%s  [%s]" % (
        shown, rec.get("user", "?"), rec.get("allowed_prefixes"), status))
PY
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
  # Print the revoked tokens REDACTED (don't echo full secrets to the terminal).
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    if [ "${#t}" -gt 14 ]; then printf '  %s…%s\n' "${t:0:8}" "${t: -4}"; else printf '  %s\n' "$t"; fi
  done <<EOF
$removed
EOF
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
  # Pass the token via env (not argv) so it isn't visible in process listings.
  MCP_TOKEN="$token" docker run --rm -i -e MCP_TOKEN --network "$NET" "$SERVER_IMAGE" \
    python - "http://docs-mcp:8080/mcp" "$thost" <<'PY'
import asyncio, os, sys
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
url, host = sys.argv[1], sys.argv[2]
token = os.environ["MCP_TOKEN"]
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
  ${C_B}token${C_0} <user> [prefix...] [--expires <Nd|Nh|never>]  mint a bearer token (default: / , 90d)
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
