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
INGEST_IMAGE="docs-mcp:ingest"     # heavy build-path image (bakes models/ at build time)
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

# --- vendored Docling models (Git LFS) ---------------------------------------
# models/ lives in Git LFS and is BAKED into the ingest image at build time.
# Every recurring "PDF ingest fails with JSONDecodeError: Expecting value: line 1
# column 1 (char 0)" incident traces back to this directory being broken in one
# of four ways, none of which Docling surfaces before conversion time:
#   missing    — checkout without the directory (or it lost its files)
#   pointer    — cloned without git-lfs installed: git silently checks out the
#                small pointer TEXT files instead of the weights (no error!)
#   empty      — a `git lfs pull` that died mid-checkout (ssh-agent had no key,
#                network drop, Ctrl-C) leaves 0-byte files behind
#   truncated  — same, but the download stopped partway through a file
#
# list_bad_models prints one "reason<TAB>path" line per broken file (empty output
# = healthy). Inside a git checkout it compares each file's on-disk size against
# the size recorded in its committed LFS pointer — authoritative, catches all four
# states. Outside git (tarball copy) it falls back to pointer-signature and
# empty-file heuristics. The Dockerfile keeps its own pointer/empty guards as a
# last line of defense for builds that bypass this script.
list_bad_models() {
  local dir="$ROOT/models"
  [ -d "$dir" ] || { printf 'missing\tmodels/ (directory absent)\n'; return 0; }
  find "$dir" -type f -print -quit 2>/dev/null | grep -q . \
    || { printf 'missing\tmodels/ (directory has no files)\n'; return 0; }
  {
    if command -v git >/dev/null 2>&1 \
       && git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      local f expected actual
      while IFS= read -r f; do
        # The blob git stores for an LFS-tracked file is a small pointer recording
        # the real content size; files kept OUT of LFS (e.g. nested .gitattributes)
        # have no "size" line and are skipped.
        expected="$(git -C "$ROOT" cat-file blob "HEAD:$f" 2>/dev/null \
                      | awk '/^size [0-9]+$/ {print $2; exit}')" || true
        [ -n "$expected" ] || continue
        if [ ! -f "$ROOT/$f" ]; then printf 'missing\t%s\n' "$f"; continue; fi
        actual="$(wc -c < "$ROOT/$f" | tr -d '[:space:]')"
        [ "$actual" = "$expected" ] && continue
        if [ "$actual" = 0 ]; then
          printf 'empty (0 of %s bytes)\t%s\n' "$expected" "$f"
        elif head -c 200 "$ROOT/$f" 2>/dev/null | grep -q 'git-lfs.github.com/spec'; then
          printf 'LFS pointer (not the %s-byte model)\t%s\n' "$expected" "$f"
        else
          printf 'truncated (%s of %s bytes)\t%s\n' "$actual" "$expected" "$f"
        fi
      done < <(git -C "$ROOT" ls-files -- models/)
    fi
    # Heuristics: the only signal outside a git checkout — and they also catch
    # stray UNtracked files (which the Dockerfile build guard rejects too).
    grep -rIl 'git-lfs.github.com/spec' "$dir" 2>/dev/null \
      | sed "s,^$ROOT/,," | awk '{printf "LFS pointer (not materialized)\t%s\n", $0}' || true
    find "$dir" -type f -empty 2>/dev/null \
      | sed "s,^$ROOT/,," | awk '{printf "empty (0 bytes)\t%s\n", $0}' || true
  } | awk -F'\t' '!seen[$2]++'   # git check is richer; drop duplicate heuristic hits
}

# Re-materialize broken vendored models in place, from Git LFS:
#   configure filters — a clone made without git-lfs has NO smudge filter in
#                       .git/config (the silent way pointers end up on disk)
#   lfs fetch         — download objects missing from .git/lfs (models/ only,
#                       so a repair never burns quota on the raw/ corpus)
#   rm + git checkout — delete the broken working copies, then restore them
#                       through the smudge filter. The rm matters: a pointer
#                       file on disk is byte-identical to the committed blob, so
#                       git treats it as CLEAN and a bare checkout would skip it.
# Returns non-zero (with the cause classified: ssh auth vs LFS bandwidth quota)
# when repair cannot run or did not work; callers re-verify with list_bad_models.
repair_models() {
  if ! command -v git >/dev/null 2>&1 \
     || ! git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    warn "models/ is broken and this is not a git checkout — copy a known-good models/ (or .git/lfs) from another machine, or re-clone with git-lfs installed."
    return 1
  fi
  if ! git lfs version >/dev/null 2>&1; then
    warn "git-lfs is not installed on this host — install it (macOS: brew install git-lfs; Debian/Ubuntu: sudo apt-get install -y git-lfs), then re-run."
    return 1
  fi
  info "repairing models/ from Git LFS (fetch missing objects + re-checkout)…"
  # Same filter config `git lfs install --local` writes, without its hook step
  # (which aborts on hosts that already have non-LFS git hooks).
  git -C "$ROOT" config filter.lfs.required true
  git -C "$ROOT" config filter.lfs.clean 'git-lfs clean -- %f'
  git -C "$ROOT" config filter.lfs.smudge 'git-lfs smudge -- %f'
  git -C "$ROOT" config filter.lfs.process 'git-lfs filter-process'
  local f out
  while IFS=$'\t' read -r _ f; do
    [ -f "$ROOT/$f" ] && rm -f -- "$ROOT/$f"
  done < <(list_bad_models)
  if ! out="$( { git -C "$ROOT" lfs fetch origin --include='models/**' \
                 && git -C "$ROOT" checkout -- models/ ; } 2>&1 )"; then
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    case "$out" in
      *"Permission denied"*|*publickey*|*"Authentication failed"*|*"could not read Username"*)
        warn "Git LFS could not AUTHENTICATE to the remote — LFS downloads authorize over the same ssh as git. Make 'ssh -T git@github.com' work non-interactively: a 'Host github.com' block with your IdentityFile in ~/.ssh/config survives reboots; a key that is only in ssh-agent does not." ;;
      *quota*|*bandwidth*)
        warn "GitHub LFS bandwidth quota exhausted (free plan: 1 GiB/month; a full models/ pull is ~530 MB). Wait for the monthly reset, buy a data pack, or copy models/ (or .git/lfs/objects) from a machine that already has them." ;;
    esac
    return 1
  fi
  return 0
}

# Gate build/ingest on healthy models, attempting in-place repair first.
# Set LFS_AUTO_REPAIR=false (.env) to report-and-stop instead of repairing.
check_lfs_models() {
  local bad
  bad="$(list_bad_models)"
  [ -z "$bad" ] && return 0
  warn "broken vendored Docling models detected:"
  printf '%s\n' "$bad" | awk -F'\t' '{printf "    %s  [%s]\n", $2, $1}' >&2
  if is_true "${LFS_AUTO_REPAIR:-true}"; then
    if repair_models; then
      bad="$(list_bad_models)"
      if [ -z "$bad" ]; then
        info "models/ repaired and verified — continuing."
        return 0
      fi
      warn "still broken after repair:"
      printf '%s\n' "$bad" | awk -F'\t' '{printf "    %s  [%s]\n", $2, $1}' >&2
    fi
  else
    info "skipping auto-repair (LFS_AUTO_REPAIR=false)"
  fi
  die "models/ is not usable — Docling loads these files at conversion time, so every PDF would fail with 'JSONDecodeError: Expecting value: line 1 column 1 (char 0)'. Manual fix: git lfs install && git lfs pull --include='models/**' && ./docmcp.sh models   (no git checkout here? copy a known-good models/ from another machine)"
}

# The models Docling ACTUALLY loads are the ones baked into the ingest image —
# a stale image built from a broken checkout keeps failing even after the
# working tree is fixed. Prints the broken in-image paths; rc 1 if any.
check_image_models() {
  docker run --rm --entrypoint sh "$INGEST_IMAGE" -c '
    bad="$(find /opt/docling/models -type f -empty 2>/dev/null;
           grep -rIl "git-lfs.github.com/spec" /opt/docling/models 2>/dev/null)"
    [ -z "$bad" ] || { printf "%s\n" "$bad"; exit 1; }'
}

# Use `image ls -q` (not `image inspect <name>`): under Docker Desktop's containerd
# image store, `inspect` by short name:tag can spuriously report "No such image"
# even when the image exists and runs fine.
server_image_exists() { [ -n "$(docker image ls -q "$SERVER_IMAGE" 2>/dev/null)" ]; }
ingest_image_exists() { [ -n "$(docker image ls -q "$INGEST_IMAGE" 2>/dev/null)" ]; }

is_running() {
  local id; id="$(dc ps -q "$1" 2>/dev/null)" || return 1
  [ -n "$id" ] || return 1
  [ "$(docker inspect -f '{{.State.Running}}' "$id" 2>/dev/null)" = "true" ]
}

# Make an auth change (tokens.json / groups.json) take effect in the running containers.
# Both files are bind-mounted into docs-mcp AND the portal as SINGLE FILES, and our writes
# are atomic (temp + os.replace), which gives the host file a NEW inode. A single-file bind
# mount stays pinned to the inode present at container start, so the container keeps reading
# the OLD file until it is restarted — and the in-process mtime live-reload can never fire,
# because the stale inode's mtime never changes. (Mounting the file is the only option: you
# cannot create a mountpoint inside the :ro docstore volume.) So an auth change only lands
# after a restart. We restart BOTH services: docs-mcp (read RBAC) and, when up, the portal
# (login + write RBAC) — otherwise a freshly minted token gets "Invalid or expired token" at
# the portal until the next restart. Messages go to stderr so a `$(... token ...)` capture
# still gets only the token on stdout.
reload_auth_services() {
  local what="${1:-auth change}"
  if is_running docs-mcp; then
    dc restart docs-mcp >/dev/null && info "reloaded the running server (${what} is live)" >&2
  else
    warn "start/restart the server for the ${what} to take effect: ./docmcp.sh serve"
  fi
  if is_running portal; then
    dc restart portal >/dev/null && info "reloaded the running portal (${what} is live)" >&2
  fi
}

# Non-blocking sanity check for READ prefixes (token positional args / group prefixes):
# warn about any that match NO document in the current index — almost always a typo
# (missing leading slash, wrong case). It NEVER blocks and NEVER changes exit status:
# folders are legitimately granted before they hold content, and the index may not be
# built yet. WRITE (--write) prefixes are intentionally exempt — granting an upload
# target for a folder that does not exist yet is a normal workflow. Uses the SAME
# segment-aware rbac.is_allowed the server enforces, so "matches nothing" here means
# "grants nothing" there. All output goes to stderr so a `$(... token ...)` capture is
# unaffected.
warn_unknown_prefixes() {
  [ "$#" -ge 1 ] || return 0
  server_image_exists || return 0
  docker volume inspect "$DOCSTORE_VOL" >/dev/null 2>&1 || {
    info "skipped prefix check — no curated store yet (run ./docmcp.sh ingest)" >&2; return 0; }
  local out
  out="$(docker run --rm -i -v "$DOCSTORE_VOL:/srv/docs:ro" "$SERVER_IMAGE" \
    python - "$@" <<'PY'
import json, os, sys
try:
    from docmcp.rbac import is_allowed                # authoritative, segment-aware
except Exception:                                     # fallback mirrors rbac.is_allowed
    import posixpath
    def is_allowed(path, prefixes):
        p = posixpath.normpath("/" + path.strip().strip("/"))
        for pref in prefixes:
            if pref.strip() in ("", "/"):
                return True
            q = posixpath.normpath("/" + pref.strip().strip("/"))
            if p == q or p.startswith(q + "/"):
                return True
        return False
ij = "/srv/docs/index.json"
docs = json.load(open(ij)) if os.path.exists(ij) else []
if not docs:
    print("__EMPTY__"); sys.exit(0)
paths = [d.get("path", "") for d in docs if isinstance(d, dict)]
for pref in sys.argv[1:]:
    if not any(is_allowed(p, [pref]) for p in paths):
        print(pref)
PY
)" || return 0
  if [ "$out" = "__EMPTY__" ]; then
    info "skipped prefix check — index is empty (run ./docmcp.sh ingest)" >&2; return 0
  fi
  [ -n "$out" ] || return 0
  local _p
  while IFS= read -r _p; do
    [ -n "$_p" ] || continue
    warn "read prefix '$_p' matches no document in the current index — check for a typo (leading slash, case); ignore if it is a folder you have not ingested yet"
  done <<EOF
$out
EOF
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
  [ -f "$ROOT/groups.json" ] || ( umask 077; printf '{}\n' > "$ROOT/groups.json" )  # RBAC groups (bind-mounted)
  # A session secret for the optional portal (only used when PORTAL_ENABLED=true).
  grep -qE '^SESSION_SECRET=.+' "$ROOT/.env" 2>/dev/null \
    || printf '\n# Portal session-cookie HMAC key (auto-generated).\nSESSION_SECRET=%s\n' \
         "$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48)" >> "$ROOT/.env"
  load_env

  info "Building the server image (one-time; usually 1–3 minutes)…"
  dc build docs-mcp

  if [ ! -f "$ROOT/tokens.json" ]; then
    ( umask 077; printf '{}\n' > "$ROOT/tokens.json" ); chmod 600 "$ROOT/tokens.json"
    local tok
    # Admin is the break-glass token: full access, non-expiring. Mint scoped,
    # expiring tokens for everyone else.
    tok="$(cmd_token admin --all --expires never)" || die "failed to create the admin token (see the Docker error above)"
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
  # The working tree is verified; now make sure the image we are about to RUN was
  # not baked from an earlier broken checkout (its models are the ones Docling uses).
  if ingest_image_exists; then
    local imgbad
    if ! imgbad="$(check_image_models)"; then
      [ -n "$imgbad" ] && printf '%s\n' "$imgbad" | sed 's,^/opt/docling/models/,    models/,' >&2
      die "the ingest image ($INGEST_IMAGE) was built from a broken models/ — the files above are empty/pointers INSIDE the image, so every PDF fails there. The working tree is good now; rebuild: ./docmcp.sh build ingest"
    fi
  fi
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
  # Ensure groups.json exists so its read-only bind mount maps a FILE (a missing
  # bind source would be created as a directory). Harmless for upgrades.
  [ -f "$ROOT/groups.json" ] || ( umask 077; printf '{}\n' > "$ROOT/groups.json" )
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
  if is_true "${PORTAL_ENABLED:-false}"; then
    [ -n "${SESSION_SECRET:-}" ] \
      || die "PORTAL_ENABLED=true but SESSION_SECRET is empty — set it in .env (./docmcp.sh setup generates one)"
    # Session cookies must be protected. Start the portal when EITHER a real TLS
    # hostname is configured (Caddy terminates HTTPS and the portal sets Secure
    # cookies) OR the operator opted into plaintext on a trusted/VPN network. The die
    # message used to promise the DOMAIN path but the code only honored the flag.
    local portal_tls=""
    case "${DOMAIN:-}" in ""|:*) portal_tls="" ;; *) portal_tls=1 ;; esac
    if [ -n "$portal_tls" ]; then
      # A TLS DOMAIN must keep Secure cookies on. Refuse the contradictory combo rather
      # than silently weaken it (the portal sets secure = NOT allow_plaintext_portal).
      if is_true "${ALLOW_PLAINTEXT_PORTAL:-false}"; then
        die "DOMAIN=${DOMAIN} serves HTTPS, but ALLOW_PLAINTEXT_PORTAL=true would disable the Secure flag on portal session cookies — unset ALLOW_PLAINTEXT_PORTAL so cookies stay Secure over TLS."
      fi
    elif is_true "${ALLOW_PLAINTEXT_PORTAL:-false}"; then
      :  # conscious plaintext opt-in for a trusted/VPN network (cookies not encrypted)
    else
      die "PORTAL_ENABLED=true needs a TLS DOMAIN=<host> (HTTPS) OR ALLOW_PLAINTEXT_PORTAL=true (trusted/VPN) — refusing to serve session cookies unprotected by default"
    fi
    dc --profile portal up -d portal
    info "Upload portal is live at: ${C_B}$(public_url | sed 's,/mcp$,/portal,')${C_0}"
    [ -n "$portal_tls" ] \
      || warn "the portal is a WRITE surface with browser session cookies over plain HTTP — keep it on a trusted/VPN network only."
  fi
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
  dc --profile ingest --profile vector --profile portal down
}

# logs  — follow the server + proxy logs.
cmd_logs() { need_docker; dc logs -f --tail=100 docs-mcp caddy; }

# build [server|ingest|all]  — (re)build images after code changes.
cmd_build() {
  need_docker; load_env   # load_env so LFS_AUTO_REPAIR from .env reaches check_lfs_models
  case "${1:-server}" in
    server) dc build docs-mcp ;;
    ingest) check_lfs_models; dc --profile ingest build ingest ;;
    all)    check_lfs_models; dc build docs-mcp && dc --profile ingest build ingest ;;
    *)      die "usage: ./docmcp.sh build [server|ingest|all]" ;;
  esac
}

# token <user> <prefix> [<prefix> ...] [--expires <Nd|Nh|Nm|never>] [--comment <text>]
#   — mint a scoped bearer token. A scope is REQUIRED: pass explicit prefixes, or
# --all for the whole corpus (admin/break-glass) — it never silently defaults to "/".
# Default expiry: 90 days (override TOKEN_TTL or pass --expires; 'never' = non-expiring).
# The record stores created_at/created_by and an optional --comment (shown by token-list).
cmd_token() {
  [ "$#" -ge 1 ] || die "usage: ./docmcp.sh token <user> [<allowed-prefix> ...] [--expires <Nd|Nh|Nm|never>]"
  need_docker
  server_image_exists || die "build the image first: ./docmcp.sh setup"
  [ -f "$ROOT/tokens.json" ] || { ( umask 077; printf '{}\n' > "$ROOT/tokens.json" ); }
  [ -w "$ROOT/tokens.json" ] || die "tokens.json is not writable: $ROOT/tokens.json"

  # Pull --expires / --comment / --group / --all out of the args; the rest is user + prefixes.
  local expires_spec="${TOKEN_TTL:-90d}" comment="" grant_all="" groups_csv="" writes_csv="" rest=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --expires)     expires_spec="${2:-}"; shift 2 || die "--expires needs a value" ;;
      --expires=*)   expires_spec="${1#--expires=}"; shift ;;
      --comment)     comment="${2:-}"; shift 2 || die "--comment needs a value" ;;
      --comment=*)   comment="${1#--comment=}"; shift ;;
      --group)       groups_csv="${groups_csv}${2:-}," ; shift 2 || die "--group needs a name" ;;
      --group=*)     groups_csv="${groups_csv}${1#--group=}," ; shift ;;
      --write)       writes_csv="${writes_csv}${2:-}," ; shift 2 || die "--write needs a prefix" ;;
      --write=*)     writes_csv="${writes_csv}${1#--write=}," ; shift ;;
      --all|--admin) grant_all=1; shift ;;
      *)             rest+=("$1"); shift ;;
    esac
  done
  set -- "${rest[@]}"
  local user="${1:-}"; [ -n "$user" ] && shift \
    || die "usage: ./docmcp.sh token <user> <prefix...> [--group <name>] [--expires …] [--comment …] | --all"
  # Require an EXPLICIT scope: prefixes, --group, or --all. NEVER silently default to
  # "/" (a full-access footgun); reject empty and bare-"/" prefixes.
  if [ -n "$grant_all" ]; then
    { [ "$#" -eq 0 ] && [ -z "$groups_csv" ]; } || die "use --all alone (not with prefixes/--group)"
    set -- "/"
  else
    { [ "$#" -ge 1 ] || [ -n "$groups_csv" ] || [ -n "$writes_csv" ]; } \
      || die "specify a read prefix (e.g. /public), --group, --write, or --all — a scope is required"
    local _p
    for _p in "$@"; do
      [ -n "$(printf '%s' "$_p" | tr -d '[:space:]')" ] \
        || die "empty prefix not allowed — pass a real path like /public (or --all)"
      [ -n "$(printf '%s' "$_p" | tr -d '/[:space:]')" ] \
        || die "a bare '/' grants the WHOLE corpus — use --all to do that explicitly"
    done
    warn_unknown_prefixes "$@"   # non-blocking typo guard for read prefixes (skips --all)
  fi

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
  # Run as the host user so the bind-mounted tokens.json stays host-owned (Linux).
  # The write is ATOMIC (temp + os.replace) so the live-reloading server (which
  # watches tokens.json's mtime) never reads a half-written file. TOKEN_BY records
  # who minted it (provenance) without putting it in argv.
  tok="$(TOKEN_BY="$(id -un 2>/dev/null || echo "${USER:-operator}")" TOKEN_COMMENT="$comment" TOKEN_GROUPS="$groups_csv" TOKEN_WRITE="$writes_csv" \
    docker run --rm -i --user "$(id -u):$(id -g)" -e TOKEN_BY -e TOKEN_COMMENT -e TOKEN_GROUPS -e TOKEN_WRITE \
    -v "$ROOT:/work" "$SERVER_IMAGE" \
    python - /work/tokens.json "$user" "$ttl" "$@" <<'PY'
import fcntl, json, os, secrets, sys, tempfile, time
path, user, ttl = sys.argv[1], sys.argv[2], sys.argv[3]
prefixes = sys.argv[4:]
groups = [g for g in (os.environ.get("TOKEN_GROUPS") or "").split(",") if g]
writes = [w.strip() for w in (os.environ.get("TOKEN_WRITE") or "").split(",") if w.strip()]
if not prefixes and not groups and not writes:  # shell guarantees a scope; refuse if empty
    sys.stderr.write("internal error: no scope given\n"); sys.exit(2)
d = os.path.dirname(path) or "."
# Serialize concurrent token writers (mint/revoke) so a read-modify-write cannot lose
# updates. flock a sibling lock file (NOT the file we os.replace); released on exit.
_lock = open(os.path.join(d, ".tokens.lock"), "a")
fcntl.flock(_lock, fcntl.LOCK_EX)
with open(path) as fh:
    data = json.load(fh) if os.path.getsize(path) > 0 else {}
rec = {
    "user": user,
    "created_at": int(time.time()),
    "created_by": os.environ.get("TOKEN_BY") or "operator",
}
if prefixes:
    rec["allowed_prefixes"] = prefixes
if groups:
    rec["groups"] = groups
if writes:
    rec["writable_prefixes"] = writes
_comment = (os.environ.get("TOKEN_COMMENT") or "").strip()
if _comment:
    rec["comment"] = _comment
if ttl:
    rec["expires_at"] = int(time.time()) + int(ttl)
tok = "tok_%s_%s" % (user, secrets.token_hex(12))
data[tok] = rec
# Atomic publish: write a temp file in the same dir (0600) then os.replace.
fd, tmp = tempfile.mkstemp(dir=d, prefix=".tokens.", suffix=".tmp")
try:
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.flush(); os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
except BaseException:
    os.unlink(tmp); raise
# Append-only audit (never the token string).
try:
    adir = os.path.join(d, "var"); os.makedirs(adir, exist_ok=True)
    with open(os.path.join(adir, "token-audit.jsonl"), "a") as af:
        af.write(json.dumps({"ts": int(time.time()), "action": "create", "user": user,
                             "by": rec["created_by"], "prefixes": prefixes, "groups": groups,
                             "writable": writes}) + "\n")
except OSError:
    pass
print(tok)
PY
)"
  printf '%s\n' "$tok"
  # Notes go to stderr so a caller capturing `$(... token ...)` gets only the token.
  if [ -n "$ttl" ]; then info "expires in ${expires_spec}" >&2; else info "non-expiring token" >&2; fi
  reload_auth_services "token"
}

# token-list [--expired]  — show configured tokens with the secret REDACTED
# (user, prefixes, expiry, created_by, comment). Never prints the full token
# string. With --expired, show only tokens whose expires_at is in the past.
cmd_token_list() {
  local tokfile="$ROOT/tokens.json"
  [ -f "$tokfile" ] || die "no tokens.json yet — run: ./docmcp.sh setup"
  need_docker
  server_image_exists || die "build the image first: ./docmcp.sh setup"
  local only="all"
  case "${1:-}" in
    --expired) only="expired" ;;
    "")        : ;;
    *)         die "usage: ./docmcp.sh token-list [--expired]" ;;
  esac
  docker run --rm -i -v "$tokfile:/work/tokens.json:ro" "$SERVER_IMAGE" \
    python - /work/tokens.json "$only" <<'PY'
import json, os, sys, time
path = sys.argv[1]
only = sys.argv[2] if len(sys.argv) > 2 else "all"
data = json.load(open(path)) if os.path.getsize(path) > 0 else {}
if not data:
    print("(no tokens)"); raise SystemExit
now = time.time()
shown_any = False
for tok, rec in data.items():
    exp = rec.get("expires_at")
    expired = bool(exp and exp < now)
    if only == "expired" and not expired:
        continue
    shown = (tok[:8] + "…" + tok[-4:]) if len(tok) > 14 else "…"
    if exp:
        status = "EXPIRED" if expired else "expires " + time.strftime("%Y-%m-%d", time.localtime(exp))
    else:
        status = "no expiry"
    extra = ""
    if rec.get("groups"):
        extra += "  groups=%s" % rec["groups"]
    if rec.get("writable_prefixes"):
        extra += "  write=%s" % rec["writable_prefixes"]
    if rec.get("created_by"):
        extra += "  by=%s" % rec["created_by"]
    if rec.get("comment"):
        extra += "  # %s" % rec["comment"]
    print("  %-16s  user=%-12s  prefixes=%s  [%s]%s" % (
        shown, rec.get("user", "?"), rec.get("allowed_prefixes") or [], status, extra))
    shown_any = True
if not shown_any:
    print("(no expired tokens)" if only == "expired" else "(no tokens)")
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
    -v "$ROOT:/work" "$SERVER_IMAGE" \
    python - /work/tokens.json "$target" <<'PY'
import fcntl, json, os, sys, tempfile
path, target = sys.argv[1], sys.argv[2]
d = os.path.dirname(path) or "."
# Serialize with concurrent mint/revoke (flock a sibling lock file) so a revoke cannot
# race a mint and lose updates; released on process exit.
_lock = open(os.path.join(d, ".tokens.lock"), "a")
fcntl.flock(_lock, fcntl.LOCK_EX)
with open(path) as fh:
    data = json.load(fh) if os.path.getsize(path) > 0 else {}
if target in data:                       # exact token string
    removed = [target]
else:                                    # otherwise treat it as a user name
    removed = [t for t, r in data.items() if isinstance(r, dict) and r.get("user") == target]
# Capture users BEFORE deletion so the audit never has to log a token string.
removed_users = sorted({data[t].get("user", "?") for t in removed if isinstance(data.get(t), dict)})
for t in removed:
    del data[t]
# Atomic publish (temp + os.replace) so the live-reloading server never reads a
# half-written file mid-revocation.
fd, tmp = tempfile.mkstemp(dir=d, prefix=".tokens.", suffix=".tmp")
try:
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.flush(); os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
except BaseException:
    os.unlink(tmp); raise
try:                                     # append-only audit (never the token string)
    import time as _t
    adir = os.path.join(d, "var"); os.makedirs(adir, exist_ok=True)
    with open(os.path.join(adir, "token-audit.jsonl"), "a") as af:
        af.write(json.dumps({"ts": int(_t.time()), "action": "revoke",
                             "users": removed_users, "count": len(removed)}) + "\n")
except OSError:
    pass
print("\n".join(removed))
PY
)" || die "failed to update tokens.json"
  [ -n "$removed" ] || die "no token or user matching '$target' (see: ./docmcp.sh token-list)"
  info "revoked:"
  # Print the revoked tokens REDACTED (don't echo full secrets to the terminal).
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    if [ "${#t}" -gt 14 ]; then printf '  %s…%s\n' "${t:0:8}" "${t: -4}"; else printf '  …\n'; fi
  done <<EOF
$removed
EOF
  reload_auth_services "revocation"
}

# group <name> <prefix> [<prefix> ...]  — define/update an RBAC group (a named set of
# read prefixes) in groups.json. Tokens reference it via `token <user> --group <name>`,
# so adding a folder to the group grants it to everyone in the group.
cmd_group() {
  [ "$#" -ge 2 ] || die "usage: ./docmcp.sh group <name> <prefix> [<prefix> ...]"
  need_docker; server_image_exists || die "build the image first: ./docmcp.sh setup"
  local name="$1"; shift
  case "$name" in ""|*[!A-Za-z0-9_-]*) die "group name must match [A-Za-z0-9_-]+" ;; esac
  local _p
  for _p in "$@"; do
    [ -n "$(printf '%s' "$_p" | tr -d '[:space:]')" ] || die "empty prefix not allowed"
    [ -n "$(printf '%s' "$_p" | tr -d '/[:space:]')" ] \
      || die "a bare '/' grants the WHOLE corpus — a group cannot hold it; use 'token <user> --all' for break-glass"
  done
  warn_unknown_prefixes "$@"   # non-blocking typo guard for the group's read prefixes
  docker run --rm -i --user "$(id -u):$(id -g)" -v "$ROOT:/work" "$SERVER_IMAGE" \
    python - /work/groups.json "$name" "$@" <<'PY'
import fcntl, json, os, sys, tempfile
path, name = sys.argv[1], sys.argv[2]
prefixes = sys.argv[3:]
d = os.path.dirname(path) or "."
lock = open(os.path.join(d, ".tokens.lock"), "a"); fcntl.flock(lock, fcntl.LOCK_EX)
data = json.load(open(path)) if os.path.exists(path) and os.path.getsize(path) > 0 else {}
data[name] = prefixes
fd, tmp = tempfile.mkstemp(dir=d, prefix=".groups.", suffix=".tmp")
try:
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True); fh.flush(); os.fsync(fh.fileno())
    os.chmod(tmp, 0o600); os.replace(tmp, path)
except BaseException:
    os.unlink(tmp); raise
print("group %s = %s" % (name, prefixes))
PY
  info "group '$name' saved."
  reload_auth_services "group change"
}

# group-list  — show the defined groups and their prefixes.
cmd_group_list() {
  need_docker; server_image_exists || die "build the image first: ./docmcp.sh setup"
  docker run --rm -i -v "$ROOT:/work:ro" "$SERVER_IMAGE" \
    python - /work/groups.json <<'PY'
import json, os, sys
p = sys.argv[1]
data = json.load(open(p)) if os.path.exists(p) and os.path.getsize(p) > 0 else {}
if not data:
    print("(no groups — define one: ./docmcp.sh group <name> <prefix> ...)"); raise SystemExit
for name, prefixes in sorted(data.items()):
    print("  %-16s %s" % (name, prefixes))
PY
}

# group-rm <name>  — delete a group. Tokens referencing it lose those prefixes.
cmd_group_rm() {
  [ "$#" -ge 1 ] || die "usage: ./docmcp.sh group-rm <name>"
  need_docker; server_image_exists || die "build the image first: ./docmcp.sh setup"
  docker run --rm -i --user "$(id -u):$(id -g)" -v "$ROOT:/work" "$SERVER_IMAGE" \
    python - /work/groups.json "$1" <<'PY'
import fcntl, json, os, sys, tempfile
path, name = sys.argv[1], sys.argv[2]
d = os.path.dirname(path) or "."
lock = open(os.path.join(d, ".tokens.lock"), "a"); fcntl.flock(lock, fcntl.LOCK_EX)
data = json.load(open(path)) if os.path.exists(path) and os.path.getsize(path) > 0 else {}
if name not in data:
    print("no such group: %s" % name); raise SystemExit
del data[name]
fd, tmp = tempfile.mkstemp(dir=d, prefix=".groups.", suffix=".tmp")
try:
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True); fh.flush(); os.fsync(fh.fileno())
    os.chmod(tmp, 0o600); os.replace(tmp, path)
except BaseException:
    os.unlink(tmp); raise
print("removed group %s" % name)
PY
  reload_auth_services "group change"
}

# token-rotate <user>  — mint a fresh token carrying the user's existing scope
# (union of read prefixes + groups + writable_prefixes, comment, expiry style) and
# revoke the old one(s).
cmd_token_rotate() {
  [ "$#" -ge 1 ] || die "usage: ./docmcp.sh token-rotate <user>"
  need_docker; server_image_exists || die "build the image first: ./docmcp.sh setup"
  [ -f "$ROOT/tokens.json" ] || die "no tokens.json — run: ./docmcp.sh setup"
  local user="$1" newtok
  newtok="$(TOKEN_BY="$(id -un 2>/dev/null || echo "${USER:-operator}")" \
    docker run --rm -i --user "$(id -u):$(id -g)" -e TOKEN_BY -v "$ROOT:/work" "$SERVER_IMAGE" \
    python - /work/tokens.json "$user" <<'PY'
import fcntl, json, os, secrets, sys, tempfile, time
path, user = sys.argv[1], sys.argv[2]
d = os.path.dirname(path) or "."
lock = open(os.path.join(d, ".tokens.lock"), "a"); fcntl.flock(lock, fcntl.LOCK_EX)
data = json.load(open(path)) if os.path.getsize(path) > 0 else {}
old = {t: r for t, r in data.items() if isinstance(r, dict) and r.get("user") == user}
if not old:
    sys.stderr.write("no tokens for user: %s\n" % user); sys.exit(1)
prefixes, groups, writes, comment = [], [], [], None
for r in old.values():
    for p in r.get("allowed_prefixes", []) or []:
        if p not in prefixes: prefixes.append(p)
    for g in r.get("groups", []) or []:
        if g not in groups: groups.append(g)
    for w in r.get("writable_prefixes", []) or []:  # optional portal WRITE scope
        if w not in writes: writes.append(w)
    if r.get("comment") and not comment:
        comment = r.get("comment")
rec = {"user": user, "created_at": int(time.time()), "last_rotated_at": int(time.time()),
       "created_by": os.environ.get("TOKEN_BY") or "operator"}
if prefixes: rec["allowed_prefixes"] = prefixes
if groups: rec["groups"] = groups
if writes: rec["writable_prefixes"] = writes
if comment: rec["comment"] = comment
# Preserve expiry STYLE: only make the new token expiring if EVERY old one was.
if all(r.get("expires_at") for r in old.values()):
    rec["expires_at"] = int(time.time()) + 90 * 86400
newtok = "tok_%s_%s" % (user, secrets.token_hex(12))
for t in list(old): del data[t]
data[newtok] = rec
fd, tmp = tempfile.mkstemp(dir=d, prefix=".tokens.", suffix=".tmp")
try:
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2); fh.flush(); os.fsync(fh.fileno())
    os.chmod(tmp, 0o600); os.replace(tmp, path)
except BaseException:
    os.unlink(tmp); raise
try:
    adir = os.path.join(d, "var"); os.makedirs(adir, exist_ok=True)
    with open(os.path.join(adir, "token-audit.jsonl"), "a") as af:
        af.write(json.dumps({"ts": int(time.time()), "action": "rotate", "user": user,
                             "by": rec["created_by"], "replaced": len(old)}) + "\n")
except OSError:
    pass
print(newtok)
PY
)" || die "rotate failed — no tokens for '$user'? (see ./docmcp.sh token-list)"
  printf '%s\n' "$newtok"
  info "rotated $user: new token minted, previous token(s) revoked" >&2
  reload_auth_services "rotation"
}

# access-check <user> <logical-path>  — does the user's effective scope allow the path?
# Prints ALLOW/DENY and exits 0/1 (2 = unknown user). Resolves groups + RBAC, no live request.
cmd_access_check() {
  [ "$#" -ge 2 ] || die "usage: ./docmcp.sh access-check <user> </logical/path>"
  need_docker; server_image_exists || die "build the image first: ./docmcp.sh setup"
  local rc=0
  docker run --rm -i -v "$ROOT:/work:ro" "$SERVER_IMAGE" \
    python - /work/tokens.json /work/groups.json "$1" "$2" <<'PY' || rc=$?
import json, os, sys
from docmcp import rbac
from docmcp.auth import effective_prefixes
tp, gp, user, target = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
tokens = json.load(open(tp)) if os.path.exists(tp) and os.path.getsize(tp) > 0 else {}
groups = json.load(open(gp)) if os.path.exists(gp) and os.path.getsize(gp) > 0 else {}
recs = [r for r in tokens.values() if isinstance(r, dict) and r.get("user") == user]
if not recs:
    print("UNKNOWN  user=%s has no tokens" % user); sys.exit(2)
eff = []
for r in recs:
    for p in effective_prefixes(r, groups):
        if p not in eff: eff.append(p)
ok = rbac.is_allowed(target, eff)
print("%s  user=%s  path=%s  (effective: %s)" % ("ALLOW" if ok else "DENY", user, target, eff))
sys.exit(0 if ok else 1)
PY
  return "$rc"
}

# audit [N]  — show the last N (default 20) token create/revoke/rotate events.
cmd_audit() {
  local log="$ROOT/var/token-audit.jsonl"
  [ -f "$log" ] || { info "(no audit log yet: $log)"; return 0; }
  tail -n "${1:-20}" "$log"
}

# access-tree  — print the whole access model as a tree: GROUPS (their folders + which
# users belong) and USERS (each token's read scope incl. group-derived folders, and its
# write scope). Read-only; tokens are redacted. Lets an admin see who can read/write what.
cmd_access_tree() {
  need_docker; server_image_exists || die "build the image first: ./docmcp.sh setup"
  # Mount the repo dir :ro so the sibling groups.json resolves (a single-file bind of a
  # missing groups.json would create a stray directory).
  docker run --rm -i -v "$ROOT:/work:ro" "$SERVER_IMAGE" \
    python - /work/tokens.json /work/groups.json <<'PY'
import json, os, sys, time
try:
    from docmcp.auth import effective_writable_prefixes  # authoritative write resolution
except Exception:  # fallback so the tree still renders if the import path changes
    def effective_writable_prefixes(rec):
        wp = rec.get("writable_prefixes")
        return [p for p in wp if isinstance(p, str)] if isinstance(wp, list) else []

tp, gp = sys.argv[1], sys.argv[2]
tokens = json.load(open(tp)) if os.path.exists(tp) and os.path.getsize(tp) > 0 else {}
groups = json.load(open(gp)) if os.path.exists(gp) and os.path.getsize(gp) > 0 else {}
now = time.time()

def redact(t):
    return (t[:8] + "…" + t[-4:]) if len(t) > 14 else "…"
def show(p):
    return "ALL (/)" if p.strip().strip("/") == "" else p
def joinp(ps):
    return ", ".join(show(p) for p in ps) if ps else "—"

# group -> users that reference it; and references to groups that are not defined
gmembers, undefined = {}, {}
for tok, rec in tokens.items():
    if not isinstance(rec, dict):
        continue
    user = rec.get("user", "?")
    for g in (rec.get("groups") or []):
        if isinstance(g, str):
            (gmembers if g in groups else undefined).setdefault(g, set()).add(user)

print("GROUPS (%d)" % len(groups))
if not groups:
    print("  (none — define one: ./docmcp.sh group <name> <prefix> ...)")
gnames = sorted(groups)
for i, name in enumerate(gnames):
    last = (i == len(gnames) - 1)
    pipe = " " if last else "│"
    folders = [p for p in (groups.get(name) or []) if isinstance(p, str)]
    members = sorted(gmembers.get(name, set()))
    print("%s %s" % ("└─" if last else "├─", name))
    print("%s    folders: %s" % (pipe, ", ".join(folders) if folders else "(none)"))
    print("%s    members: %s" % (pipe, ", ".join(members) if members else "(none — no token references it)"))
if undefined:
    print("\n  ! tokens reference groups that are NOT defined:")
    for g in sorted(undefined):
        print("    - %s  (used by: %s) — define: ./docmcp.sh group %s <prefix> ..." % (
            g, ", ".join(sorted(undefined[g])), g))

byuser = {}
for tok, rec in tokens.items():
    if isinstance(rec, dict):
        byuser.setdefault(rec.get("user", "?"), []).append((tok, rec))

print("\nUSERS (%d)" % len(byuser))
if not byuser:
    print("  (no tokens — mint one: ./docmcp.sh token <user> <prefix> ...)")
unames = sorted(byuser)
for i, user in enumerate(unames):
    last = (i == len(unames) - 1)
    pipe = " " if last else "│"
    print("%s %s" % ("└─" if last else "├─", user))
    for tok, rec in byuser[user]:
        exp = rec.get("expires_at")
        if isinstance(exp, (int, float)):
            st = ("EXPIRED " if exp < now else "expires ") + time.strftime("%Y-%m-%d", time.localtime(exp))
        else:
            st = "no expiry"
        meta = ""
        if rec.get("created_by"):
            meta += "  by=%s" % rec["created_by"]
        if rec.get("comment"):
            meta += "  # %s" % rec["comment"]
        explicit = [p for p in (rec.get("allowed_prefixes") or []) if isinstance(p, str)]
        bits = [joinp(explicit)] if explicit else []
        for g in (rec.get("groups") or []):
            if not isinstance(g, str):
                continue
            if g in groups:
                gf = [p for p in (groups.get(g) or []) if isinstance(p, str)]
                bits.append("group:%s(%s)" % (g, ", ".join(gf) if gf else "—"))
            else:
                bits.append("group:%s(UNDEFINED)" % g)
        read_str = "  +  ".join(bits) if bits else "— (no read access)"
        print("%s    %s  [%s]%s" % (pipe, redact(tok), st, meta))
        print("%s      read : %s" % (pipe, read_str))
        print("%s      write: %s" % (pipe, joinp(effective_writable_prefixes(rec))))
PY
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
      python -c 'import json,os; p="/srv/docs/index.json"; print(len(json.load(open(p))) if os.path.exists(p) else 0)' 2>/dev/null || echo '?')"
    printf "  %-10s %s\n" "indexed" "${n} docs"
    local last
    last="$(docker run --rm -v "$DOCSTORE_VOL:/srv/docs:ro" "$SERVER_IMAGE" \
      python -c 'import json,os,time; p="/srv/docs/ingest-status.json"; s=json.load(open(p)) if os.path.exists(p) else None; print("%s (failed=%s)"%(time.strftime("%Y-%m-%d %H:%M",time.localtime(s["finished_at"])),s.get("failed","?")) if s else "never")' 2>/dev/null || echo '?')"
    printf "  %-10s %s\n" "ingest" "$last"
  else
    printf "  %-10s %s\n" "indexed" "(not built yet — run ./docmcp.sh ingest)"
  fi
}

# inventory  — corpus breakdown from the built index (totals by type + by top-level
# folder). Operator-side complement to the doc-report Codex skill (needs no client).
cmd_inventory() {
  need_docker; load_env
  server_image_exists || die "build the image first: ./docmcp.sh setup"
  docker volume inspect "$DOCSTORE_VOL" >/dev/null 2>&1 || die "no store yet — run ./docmcp.sh ingest"
  docker run --rm -i -v "$DOCSTORE_VOL:/srv/docs:ro" "$SERVER_IMAGE" \
    python - <<'PY'
import collections, json, os
ij = "/srv/docs/index.json"
docs = json.load(open(ij)) if os.path.exists(ij) else []
if not docs:
    print("(index empty — run ./docmcp.sh ingest)"); raise SystemExit
def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.0f%s" % (n, u)
        n /= 1024
    return "%.1fTB" % n
by_type, bytes_type, by_folder, total = (
    collections.Counter(), collections.Counter(), collections.Counter(), 0)
for d in docs:
    t, b = d.get("type", "?"), d.get("bytes", 0)
    by_type[t] += 1; bytes_type[t] += b; total += b
    parts = d.get("path", "/").strip("/").split("/")
    by_folder["/" + parts[0] if parts and parts[0] else "/"] += 1
print("%d documents, %s total" % (len(docs), human(total)))
print("\nby type:")
for t, c in by_type.most_common():
    print("  %-10s %4d  (%s)" % (t, c, human(bytes_type[t])))
print("\nby top-level folder:")
for f, c in sorted(by_folder.items()):
    print("  %-16s %4d" % (f, c))
PY
}

# doctor  — production health checks. Exits NON-ZERO if anything is unhealthy, so it
# can gate a deploy. Validates: server up; tokens.json + groups.json parse through the
# real token verifier (same code path as the server); index.json present+valid (+ doc
# count); the search backend; the curated store is mounted read-only; the last ingest
# result; and, when PORTAL_ENABLED, that the portal answers /healthz.
cmd_doctor() {
  need_docker; load_env
  server_image_exists || die "build the image first: ./docmcp.sh setup"
  local fail=0
  _dck() {  # name  ok(0=pass)  detail
    if [ "$2" = 0 ]; then printf "  [PASS] %-14s %s\n" "$1" "$3"
    else printf "  [FAIL] %-14s %s\n" "$1" "$3"; fail=1; fi
  }
  printf "%sdocmcp doctor%s\n" "$C_B" "$C_0"

  is_running docs-mcp && _dck server 0 "running" || _dck server 1 "not running (./docmcp.sh serve)"

  # tokens.json + groups.json validated through the SAME verifier the server uses
  # (schema-checks both; groups.json is optional). Mount the repo dir :ro so a missing
  # groups.json is simply absent (a single-file bind would create a stray directory).
  # A missing/empty tokens.json means NO ONE can authenticate, so treat it as unhealthy
  # rather than reporting a hollow "0 token(s)" as a PASS.
  local tk
  if [ ! -f "$ROOT/tokens.json" ]; then
    _dck tokens.json 1 "missing — run ./docmcp.sh setup"
  elif tk="$(docker run --rm -i -v "$ROOT:/work:ro" "$SERVER_IMAGE" \
      python - /work/tokens.json <<'PY' 2>/dev/null
import sys
from docmcp.auth import JsonFileTokenVerifier
try:
    v = JsonFileTokenVerifier(sys.argv[1])   # sibling /work/groups.json auto-loaded + validated
    if not v._digests:
        print("no tokens configured (no one can authenticate) — run ./docmcp.sh token"); sys.exit(1)
    print("%d token(s), %d group(s)" % (len(v._digests), len(v._groups)))
except Exception as e:
    print("invalid: %s" % e); sys.exit(1)
PY
  )"; then _dck tokens.json 0 "$tk"; else _dck tokens.json 1 "${tk:-unreadable/invalid}"; fi

  if docker volume inspect "$DOCSTORE_VOL" >/dev/null 2>&1; then
    local ix
    if ix="$(docker run --rm -i -v "$DOCSTORE_VOL:/srv/docs:ro" "$SERVER_IMAGE" \
        python - <<'PY' 2>/dev/null
import json, os, sys, time
root = "/srv/docs"
ij = os.path.join(root, "index.json")
if not os.path.exists(ij):
    print("no index.json (run ./docmcp.sh ingest)"); sys.exit(1)
try:
    n = len(json.load(open(ij)))
except Exception as e:
    print("index.json invalid: %s" % e); sys.exit(1)
st = os.path.join(root, "ingest-status.json")
extra = ""
if os.path.exists(st):
    s = json.load(open(st))
    extra = " | last ingest %s, failed=%s" % (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("finished_at", 0))), s.get("failed", "?"))
    if s.get("failed"):
        print("%d docs, %d FAILED last ingest%s" % (n, s["failed"], extra)); sys.exit(1)
print("%d docs%s" % (n, extra))
PY
    )"; then _dck index 0 "$ix"; else _dck index 1 "${ix:-no/invalid index}"; fi
  else
    _dck index 1 "docstore volume missing (run ./docmcp.sh ingest)"
  fi

  # Search backend actually usable? (fts5 db valid when selected; rg present otherwise)
  local backend="${SEARCH_BACKEND:-ripgrep}"
  if [ "$backend" = "fts5" ]; then
    local fts
    if docker volume inspect "$DOCSTORE_VOL" >/dev/null 2>&1 && fts="$(
        docker run --rm -i -v "$DOCSTORE_VOL:/srv/docs:ro" "$SERVER_IMAGE" \
        python - <<'PY' 2>/dev/null
import os, sqlite3, sys
db = "/srv/docs/index.sqlite"
if not os.path.exists(db):
    print("fts5 db missing (run ./docmcp.sh ingest)"); sys.exit(1)
try:
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    print("fts5 ok, %d lines" % c.execute("SELECT count(*) FROM doc_lines").fetchone()[0])
except Exception as e:
    print("fts5 invalid: %s" % e); sys.exit(1)
PY
    )"; then _dck search 0 "$fts"; else _dck search 1 "${fts:-fts5 db missing/invalid}"; fi
  else
    docker run --rm "$SERVER_IMAGE" rg --version >/dev/null 2>&1 \
      && _dck search 0 "ripgrep available" || _dck search 1 "ripgrep missing in image"
  fi

  # Vendored models — the source of the recurring "every PDF fails with
  # JSONDecodeError" incident. A broken working tree poisons the NEXT build;
  # a broken baked image poisons every ingest NOW. Read-only here (doctor never
  # mutates); repair with ./docmcp.sh models --repair.
  local mb
  mb="$(list_bad_models)"
  if [ -z "$mb" ]; then
    _dck models 0 "models/ verified ($(find "$ROOT/models" -type f 2>/dev/null | wc -l | tr -d ' ') files)"
  else
    _dck models 1 "$(printf '%s\n' "$mb" | wc -l | tr -d ' ') broken file(s) in models/ — run: ./docmcp.sh models --repair"
  fi
  if ingest_image_exists; then
    if check_image_models >/dev/null 2>&1; then
      _dck ingest-image 0 "baked models look good"
    else
      _dck ingest-image 1 "broken models INSIDE $INGEST_IMAGE — rebuild: ./docmcp.sh build ingest"
    fi
  fi

  # The curated store MUST be mounted read-only in the running server (invariant).
  if is_running docs-mcp; then
    local cid rw
    cid="$(dc ps -q docs-mcp 2>/dev/null)"
    rw="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/srv/docs"}}{{.RW}}{{end}}{{end}}' "$cid" 2>/dev/null)"
    case "$rw" in
      false) _dck docstore-ro 0 "curated mounted read-only" ;;
      true)  _dck docstore-ro 1 "curated is WRITABLE in docs-mcp — must be :ro" ;;
      *)     _dck docstore-ro 1 "could not determine /srv/docs mount mode" ;;
    esac
  fi

  # Portal (optional write surface): when enabled it must be up and answer /healthz.
  if is_true "${PORTAL_ENABLED:-false}"; then
    if is_running portal; then
      if docker run --rm -i --network "$NET" "$SERVER_IMAGE" python - <<'PY' >/dev/null 2>&1
import sys, urllib.request
try:
    r = urllib.request.urlopen("http://portal:8080/healthz", timeout=5)
    sys.exit(0 if (r.status == 200 and r.read().strip() == b"ok") else 1)
except Exception:
    sys.exit(1)
PY
      then _dck portal 0 "healthz ok"; else _dck portal 1 "portal up but /healthz failed"; fi
    else
      _dck portal 1 "PORTAL_ENABLED but the portal container is not running (./docmcp.sh serve)"
    fi
  fi

  if [ "$fail" = 0 ]; then info "healthy"; return 0; else warn "unhealthy"; return 1; fi
}

# models [--repair]  — verify the vendored Docling models: the working tree
# (materialized? right size per each committed LFS pointer?) AND, when built,
# the copy baked into the ingest image. Read-only by default; exits non-zero
# when broken. --repair re-materializes from Git LFS first (fetch + checkout).
cmd_models() {
  local do_repair=""
  case "${1:-}" in
    --repair) do_repair=1 ;;
    "")       ;;
    *)        die "usage: ./docmcp.sh models [--repair]" ;;
  esac
  local bad
  bad="$(list_bad_models)"
  if [ -n "$bad" ] && [ -n "$do_repair" ]; then
    warn "broken model files:"
    printf '%s\n' "$bad" | awk -F'\t' '{printf "    %s  [%s]\n", $2, $1}' >&2
    repair_models || true
    bad="$(list_bad_models)"
  fi
  if [ -z "$bad" ]; then
    info "models/ OK — $(find "$ROOT/models" -type f 2>/dev/null | wc -l | tr -d ' ') files, all materialized (no pointers, empties, or size mismatches)."
  else
    printf '%s\n' "$bad" | awk -F'\t' '{printf "    %s  [%s]\n", $2, $1}' >&2
    if [ -n "$do_repair" ]; then
      die "repair did not fix the files above — see the repair messages (ssh auth / LFS quota / missing git-lfs)."
    fi
    die "models/ is broken (details above) — fix with: ./docmcp.sh models --repair"
  fi
  # Baked-image half — informational; skipped when docker is down or no image yet.
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && ingest_image_exists; then
    local imgbad
    if imgbad="$(check_image_models)"; then
      info "ingest image ($INGEST_IMAGE): baked models OK."
    else
      [ -n "$imgbad" ] && printf '%s\n' "$imgbad" | sed 's,^/opt/docling/models/,    models/,' >&2
      die "the ingest image was built from a broken models/ (the files above are broken INSIDE the image) — rebuild: ./docmcp.sh build ingest"
    fi
  else
    info "(ingest image not built yet, or docker not running — skipped the baked-image check)"
  fi
}

# backup [<dir>]  — snapshot the irreplaceable, NOT-version-controlled state into a
# timestamped 0600 tar.gz (default: ./backups). Includes tokens.json, groups.json
# (permission-critical: group-backed tokens lose access without it), .env, the token
# audit log, the portal audit/version state (raw/.portal, gitignored), and the Caddy
# TLS-data volume (certs; only on an HTTPS/DOMAIN deploy). NOT included: raw/ source
# docs (backed up via Git LFS — push it) and the curated store + index (rebuildable
# from raw/ via ./docmcp.sh ingest, so it's treated as cache). See RUNBOOK.md.
cmd_backup() {
  need_docker
  server_image_exists || die "build the image first: ./docmcp.sh setup"
  local dest="${1:-$ROOT/backups}"
  mkdir -p "$dest" || die "cannot create backup dir: $dest"
  local ts out staging
  ts="$(date +%Y%m%d-%H%M%S)"
  out="$dest/docmcp-backup-$ts.tar.gz"
  staging="$(mktemp -d)" || die "mktemp failed"
  # shellcheck disable=SC2064
  trap "rm -rf '$staging'" RETURN
  local included="" skipped=""
  if [ -f "$ROOT/tokens.json" ]; then cp "$ROOT/tokens.json" "$staging/"; included="$included tokens.json"; else skipped="$skipped tokens.json"; fi
  # groups.json is permission-critical, gitignored, and NOT rebuildable: a token that
  # references a group loses access if it's missing after a restore. Always capture it.
  if [ -f "$ROOT/groups.json" ]; then cp "$ROOT/groups.json" "$staging/"; included="$included groups.json"; else skipped="$skipped groups.json"; fi
  if [ -f "$ROOT/.env" ];        then cp "$ROOT/.env" "$staging/";        included="$included .env";        else skipped="$skipped .env";        fi
  # Provenance/audit that is gitignored and unrecoverable once lost (best-effort).
  if [ -f "$ROOT/var/token-audit.jsonl" ]; then mkdir -p "$staging/var"; cp "$ROOT/var/token-audit.jsonl" "$staging/var/"; included="$included token-audit"; fi
  if [ -d "$ROOT/raw/.portal" ];           then cp -R "$ROOT/raw/.portal" "$staging/raw-portal";            included="$included portal-state"; fi
  # Caddy TLS data (ACME certs) lives in the caddy_data volume; tar it via the
  # image's python (runs as root so it can read root-owned cert files).
  local cvol
  # `|| true` so a no-match grep (no caddy_data volume — e.g. a plaintext/VPN install or
  # backup before serve ever ran) does not abort the whole backup under `set -o pipefail`.
  cvol="$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep -E 'caddy_data$' | head -1 || true)"
  if [ -n "$cvol" ] && docker run --rm -v "$cvol:/data:ro" -v "$staging:/out" "$SERVER_IMAGE" \
        python -c "import tarfile; t=tarfile.open('/out/caddy_data.tar.gz','w:gz'); t.add('/data', arcname='caddy_data'); t.close()" 2>/dev/null; then
    included="$included caddy_data"
  else
    skipped="$skipped caddy_data(none/HTTPS-only)"
  fi
  tar czf "$out" -C "$staging" . || die "failed to write $out"
  chmod 600 "$out"
  info "backup written: $out ($(du -h "$out" 2>/dev/null | cut -f1))"
  info "included:${included:- (nothing)}"
  [ -n "$skipped" ] && warn "skipped:$skipped"
  info "raw/ is backed up via Git LFS (git push); curated store + index are rebuildable (./docmcp.sh ingest) — restore steps in RUNBOOK.md"
}

# schedule [<spec>|off]  — run `ingest` on a schedule. <spec> is one of:
#   30m | 2h                 every N minutes (1-59) or hours (1-23)
#   hourly | daily | weekly  presets
#   "*/15 * * * *"           a raw 5-field cron expression (quote it)
# Backend: the host `crontab` when present; otherwise a systemd timer (no `cron`
# package needed) on a systemd host run as root. No arg shows the current
# schedule; `off` removes it. Idempotent: re-running replaces our entry and
# leaves any other crontab lines / units untouched.
_cron_marker() { printf '# docmcp-ingest:%s' "$ROOT"; }
# A stable, per-deploy systemd unit name so two checkouts on one host don't
# collide (mirrors the cron marker, which is namespaced by $ROOT).
_sched_id() { printf 'docmcp-ingest-%s' "$(printf '%s' "$ROOT" | cksum | cut -d' ' -f1)"; }

# Translate a spec to "<cron-expr>|<OnCalendar>". The OnCalendar half is empty
# for a raw 5-field cron expression — only the crontab backend can run those.
# Kept pure (echoes; no side effects) so it is unit-testable.
_sched_translate() {
  local spec="$1" n
  case "$spec" in
    hourly) printf '0 * * * *|*-*-* *:00:00' ;;
    daily)  printf '0 2 * * *|*-*-* 02:00:00' ;;
    weekly) printf '0 2 * * 0|Sun *-*-* 02:00:00' ;;
    *m) n="${spec%m}"; { [ "$n" -ge 1 ] && [ "$n" -le 59 ]; } 2>/dev/null \
          || die "minutes must be 1-59: $spec"; printf '*/%s * * * *|*:0/%s' "$n" "$n" ;;
    *h) n="${spec%h}"; { [ "$n" -ge 1 ] && [ "$n" -le 23 ]; } 2>/dev/null \
          || die "hours must be 1-23: $spec"; printf '0 */%s * * *|0/%s:00' "$n" "$n" ;;
    *)  [ "$(printf '%s' "$spec" | awk '{print NF}')" = 5 ] \
          || die "usage: ./docmcp.sh schedule <Nm|Nh|hourly|daily|weekly|'m h dom mon dow'|off>"
        printf '%s|' "$spec" ;;
  esac
}

cmd_schedule() {
  local spec="${1:-}"
  case "$spec" in
    ""|status|show)  _sched_status; return ;;
    off|remove|stop) _sched_remove; return ;;
  esac
  local t cron_expr oncal
  t="$(_sched_translate "$spec")"   # die() inside propagates: set -e aborts on a failed $()
  cron_expr="${t%%|*}"; oncal="${t#*|}"
  _sched_install "$cron_expr" "$oncal"
}

# Pick a backend: host crontab first (unchanged behavior when present), else a
# systemd timer, else fail with actionable guidance.
_sched_install() {
  local cron_expr="$1" oncal="$2"
  if command -v crontab >/dev/null 2>&1; then
    _cron_install "$cron_expr"; return
  fi
  if command -v systemctl >/dev/null 2>&1; then
    [ "$(id -u)" = 0 ] \
      || die "no 'crontab' here, and writing a systemd timer needs root — re-run with sudo, or install cron (e.g. apt-get install -y cron)"
    [ -n "$oncal" ] \
      || die "the systemd-timer fallback supports presets only (daily|hourly|weekly|Nm|Nh); for a raw cron expression install cron (e.g. apt-get install -y cron) and retry"
    _systemd_install "$oncal" "$cron_expr"; return
  fi
  die "no scheduler available — install cron (e.g. apt-get install -y cron), or use a systemd host as root"
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

# systemd fallback: a oneshot .service + a .timer, enabled now. Output is
# captured by the journal (journalctl -u <unit>) — no `cron` package required.
_systemd_install() {
  local oncal="$1" cron_expr="$2" id svc tmr dockerdir
  id="$(_sched_id)"
  svc="/etc/systemd/system/${id}.service"
  tmr="/etc/systemd/system/${id}.timer"
  dockerdir="$(dirname "$(command -v docker)")"
  cat > "$svc" <<EOF
[Unit]
Description=docmcp re-ingest ($ROOT)
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Environment=PATH=$dockerdir:/usr/local/bin:/usr/bin:/bin
ExecStart=$ROOT/docmcp.sh ingest
EOF
  cat > "$tmr" <<EOF
[Unit]
Description=docmcp re-ingest schedule — OnCalendar=$oncal ($ROOT)

[Timer]
# No Persistent=true: a daily/weekly OnCalendar whose time already passed today
# would otherwise fire an immediate "catch-up" ingest on install — a surprising,
# silent (journal-only) heavy run, and unlike the cron backend. Match cron: fire
# only at the scheduled time.
OnCalendar=$oncal
Unit=${id}.service

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now "${id}.timer" >/dev/null 2>&1 \
    || die "wrote the units but failed to enable ${id}.timer — inspect: systemctl status ${id}.timer"
  info "scheduled (systemd timer) — '$cron_expr' → OnCalendar='$oncal' runs ${C_B}./docmcp.sh ingest${C_0}"
  info "  journal: journalctl -u ${id}.service   •   next run: systemctl list-timers ${id}.timer"
  info "  show: ./docmcp.sh schedule   •   remove: ./docmcp.sh schedule off"
  warn "fires only while Docker is running"
}

# Remove whatever backend(s) hold a docmcp schedule for THIS deploy.
_sched_remove() {
  local removed=0 marker current kept id
  if command -v crontab >/dev/null 2>&1; then
    marker="$(_cron_marker)"
    current="$(crontab -l 2>/dev/null || true)"
    if printf '%s\n' "$current" | grep -qF "$marker"; then
      kept="$(printf '%s\n' "$current" | grep -vF "$marker" || true)"
      if [ -n "$kept" ]; then printf '%s\n' "$kept" | crontab -; else crontab -r 2>/dev/null || true; fi
      info "schedule removed (cron)"; removed=1
    fi
  fi
  id="$(_sched_id)"
  if [ -f "/etc/systemd/system/${id}.timer" ] || [ -f "/etc/systemd/system/${id}.service" ]; then
    if command -v systemctl >/dev/null 2>&1; then
      systemctl disable --now "${id}.timer" >/dev/null 2>&1 || true
    fi
    rm -f "/etc/systemd/system/${id}.timer" "/etc/systemd/system/${id}.service" 2>/dev/null || true
    if command -v systemctl >/dev/null 2>&1; then systemctl daemon-reload >/dev/null 2>&1 || true; fi
    info "schedule removed (systemd timer ${id})"; removed=1
  fi
  if [ "$removed" != 1 ]; then info "no docmcp schedule was set"; fi
}

# Show whichever backend currently holds a schedule for THIS deploy.
_sched_status() {
  local found=0 marker line id
  if command -v crontab >/dev/null 2>&1; then
    marker="$(_cron_marker)"
    line="$(crontab -l 2>/dev/null | grep -F "$marker" || true)"
    if [ -n "$line" ]; then info "current schedule (cron):"; printf '  %s\n' "$line"; found=1; fi
  fi
  id="$(_sched_id)"
  if [ -f "/etc/systemd/system/${id}.timer" ]; then
    info "current schedule (systemd timer ${id}):"
    if command -v systemctl >/dev/null 2>&1; then systemctl list-timers "${id}.timer" --no-pager 2>/dev/null || true; fi
    found=1
  fi
  if [ "$found" != 1 ]; then info "no schedule set — e.g.: ./docmcp.sh schedule 30m   (or hourly | daily | 'm h dom mon dow')"; fi
}

# env-set <KEY> <VALUE>  — atomically set a single .env key (used by the console config
# editor; the console validates KEY against its editable allowlist before calling). Thin
# atomic writer (temp + mv, mode 600); pure awk, no `sed -i`.
cmd_env_set() {
  [ "$#" -eq 2 ] || die "usage: ./docmcp.sh env-set <KEY> <VALUE>"
  local key="$1" value="$2"
  case "$key" in
    [A-Za-z_][A-Za-z0-9_]*) ;;
    *) die "invalid env key: $key" ;;
  esac
  [ -f "$ROOT/.env" ] || { cp "$ROOT/.env.example" "$ROOT/.env"; chmod 600 "$ROOT/.env"; }
  local tmp; tmp="$(mktemp "$ROOT/.env.XXXXXX")" || die "mktemp failed"
  chmod 600 "$tmp"
  # Replace an existing KEY= line in place, else append. VALUE is passed via the
  # environment (never interpolated into the awk program) so its contents stay literal.
  if ! KEY="$key" VALUE="$value" awk '
        BEGIN { k=ENVIRON["KEY"]; v=ENVIRON["VALUE"]; done=0 }
        index($0, k "=") == 1 { print k "=" v; done=1; next }
        { print }
        END { if (!done) print k "=" v }
      ' "$ROOT/.env" > "$tmp"; then
    rm -f "$tmp"; die "failed to write .env"
  fi
  mv "$tmp" "$ROOT/.env"
  info "set ${key} in .env — restart the server for it to take effect: ./docmcp.sh serve"
}

# console [--port N] [--bind ADDR] [--build]  — launch the admin/setup web console.
# Runs as a CONTAINER on the host with the Docker socket + repo bind-mounted (so it can
# drive the full lifecycle and edit tokens/groups/.env via the docmcp.sh verbs), published
# on LOOPBACK ONLY. Requires the admin (whole-corpus) token to log in; before setup it
# starts in a one-time BOOTSTRAP mode that only serves the setup wizard.
cmd_console() {
  need_docker
  local port=8765 bind=127.0.0.1 do_build=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --port)    port="${2:-}"; shift 2 || die "--port needs a value" ;;
      --port=*)  port="${1#--port=}"; shift ;;
      --bind)    bind="${2:-}"; shift 2 || die "--bind needs a value" ;;
      --bind=*)  bind="${1#--bind=}"; shift ;;
      --build)   do_build=1; shift ;;
      *)         die "usage: ./docmcp.sh console [--port N] [--bind 127.0.0.1] [--build]" ;;
    esac
  done
  # Loopback only: the console runs Docker and edits tokens — far too powerful to expose.
  case "$bind" in
    127.0.0.1|localhost|::1) ;;
    *) die "the console runs Docker and edits tokens — it must stay on loopback (got --bind ${bind}). To reach it from another machine, tunnel it: ssh -L ${port}:127.0.0.1:${port} <host>, then open http://127.0.0.1:${port}" ;;
  esac
  [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || die "invalid --port '$port'"

  # Ensure .env + a SESSION_SECRET exist so the console can sign cookies even on a fresh
  # checkout (mirrors cmd_setup; never disturbs an existing secret).
  [ -f "$ROOT/.env" ] || { cp "$ROOT/.env.example" "$ROOT/.env"; chmod 600 "$ROOT/.env"; }
  grep -qE '^SESSION_SECRET=.+' "$ROOT/.env" 2>/dev/null \
    || printf '\n# Console/portal session-cookie HMAC key (auto-generated).\nSESSION_SECRET=%s\n' \
         "$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48)" >> "$ROOT/.env"
  load_env

  # Build the SPA in a throwaway node container (no host Node needed) on --build or first run.
  if [ -n "$do_build" ] || [ ! -f "$ROOT/console-ui/dist/index.html" ]; then
    [ -d "$ROOT/console-ui" ] || die "console-ui/ is missing — cannot build the console UI"
    info "Building the console UI (Vite, in a node:20 container)…"
    # HOME=/tmp gives npm a writable cache/log dir: running as an arbitrary host uid with no
    # home, npm otherwise fails with "error writing to the directory: /.npm/_logs" on Linux
    # (Docker Desktop on macOS masks this via uid mapping; native Ubuntu/WSL does not).
    docker run --rm -v "$ROOT/console-ui:/app" -w /app --user "$(id -u):$(id -g)" -e HOME=/tmp \
      node:20 sh -lc "npm install --no-audit --no-fund && npm run build" \
      || die "console UI build failed (see output above)"
  fi

  # Build the console image (server image + Docker CLI) if missing or --build.
  if [ -n "$do_build" ] || [ -z "$(docker image ls -q docs-mcp:console 2>/dev/null)" ]; then
    server_image_exists || { info "Building the server image first…"; dc build docs-mcp; }
    info "Building the console image (Docker CLI on the server image)…"
    docker build -t docs-mcp:console --target console -f "$ROOT/docker/Dockerfile" "$ROOT" \
      || die "console image build failed"
  fi

  # Bootstrap token: only when no usable token exists yet (pre-setup). In-memory only,
  # passed via env; the console refuses it the moment setup mints the admin token.
  local bootstrap="" url_suffix=""
  if [ ! -s "$ROOT/tokens.json" ] || ! grep -q '"user"' "$ROOT/tokens.json" 2>/dev/null; then
    bootstrap="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)"
    url_suffix="/?bootstrap=${bootstrap}"
    warn "no admin token yet — starting in BOOTSTRAP mode. Open the URL below to run the setup wizard; bootstrap access closes automatically once setup mints the admin token."
  fi

  # Docker-out-of-docker wiring. Honor DOCKER_HOST so rootless Docker
  # (unix://\$XDG_RUNTIME_DIR/docker.sock — common on Ubuntu) and remote/TCP daemons work, not
  # just the default /var/run/docker.sock that Docker Desktop (macOS / WSL) and rootful Linux use.
  local dood=()
  case "${DOCKER_HOST:-}" in
    ""|unix://*)
      local sock=/var/run/docker.sock
      [ -z "${DOCKER_HOST:-}" ] || sock="${DOCKER_HOST#unix://}"
      [ -S "$sock" ] || die "Docker socket not found at ${sock} — start Docker, or set DOCKER_HOST (rootless Docker usually sets DOCKER_HOST=unix://\$XDG_RUNTIME_DIR/docker.sock)."
      # The gid that owns the socket INSIDE the container is what the runtime enforces: Docker
      # Desktop maps it to 0; rootful Linux preserves the host docker gid through the bind mount.
      # Probe it via the console image, not host `stat` (whose gid is meaningless in the Linux VM
      # and differs BSD vs GNU).
      local sock_gid; sock_gid="$(docker run --rm -v "$sock:$sock" docs-mcp:console stat -c '%g' "$sock" 2>/dev/null || true)"
      dood+=(-v "$sock:$sock" -e "DOCKER_HOST=unix://$sock")
      [ -n "$sock_gid" ] && dood+=(--group-add "$sock_gid")
      ;;
    *)
      warn "DOCKER_HOST=${DOCKER_HOST} is not a unix socket — passing it through; the console container must be able to reach that endpoint (a routable host/IP, not localhost)."
      dood+=(-e "DOCKER_HOST=${DOCKER_HOST}")
      ;;
  esac

  info "Console: ${C_B}http://${bind}:${port}${url_suffix}${C_0}"
  info "  loopback only — stop with Ctrl-C"
  # Bind the repo at the SAME absolute path so docmcp.sh inside resolves ROOT identically
  # and compose's ../raw etc. resolve on the HOST daemon. PYTHONPATH points at the mounted
  # src so console code edits take effect without an image rebuild. ALLOW_PLAINTEXT_PORTAL
  # is forced on: the console is loopback HTTP, so session cookies must not be Secure-only.
  local tty=(); [ -t 1 ] && tty=(-t)   # allocate a TTY only when attached to one (WSL-safe)
  exec docker run --rm ${tty[@]+"${tty[@]}"} \
    --name docs-mcp-console \
    -p "${bind}:${port}:8080" \
    ${dood[@]+"${dood[@]}"} \
    -v "$ROOT:$ROOT" -w "$ROOT" \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    -e PYTHONPATH="$ROOT/src" \
    -e DOCMCP_REPO_ROOT="$ROOT" \
    -e TOKENS_FILE="$ROOT/tokens.json" \
    -e DOC_ROOT="$ROOT/raw" -e DOCSTORE_ROOT="$ROOT" \
    -e CONSOLE_STATIC_DIR="$ROOT/console-ui/dist" \
    -e SESSION_SECRET="${SESSION_SECRET:-}" \
    -e ALLOW_PLAINTEXT_PORTAL=true \
    -e CONSOLE_BOOTSTRAP_TOKEN="$bootstrap" \
    -e BIND_HOST=0.0.0.0 -e BIND_PORT=8080 \
    docs-mcp:console docmcp-console
}

# menu — a friendly interactive chooser for the common things (the web console, a local or
# remote deploy, setup, and basic ops). This is the DEFAULT when docmcp.sh is run with no
# arguments on a terminal; piped/non-interactive runs fall through to the full command list.
cmd_menu() {
  if [ ! -t 0 ] || [ ! -t 1 ]; then usage; return 0; fi
  local hint=""
  [ -s "$ROOT/tokens.json" ] || hint=" ${C_Y}(not set up yet — try 1, 2, 3, or 4)${C_0}"
  while :; do
    printf '\n%s\n\n' "${C_B}docmcp${C_0} — what would you like to do?${hint}"
    printf '  %s1%s  Web console (UI)      browser admin + setup wizard (loopback only)\n'     "$C_B" "$C_0"
    printf '  %s2%s  Deploy locally        loopback on this machine (plain HTTP, 127.0.0.1)\n' "$C_B" "$C_0"
    printf '  %s3%s  Deploy to a server    remote: VPN (plaintext) or HTTPS (hostname)\n'       "$C_B" "$C_0"
    printf '  %s4%s  Quick setup           build image + create .env + admin token\n'           "$C_B" "$C_0"
    printf '  %s5%s  Start services        serve (server + reverse proxy)\n'                    "$C_B" "$C_0"
    printf '  %s6%s  Stop services         stop\n'                                              "$C_B" "$C_0"
    printf '  %s7%s  Status & health       status + doctor\n'                                   "$C_B" "$C_0"
    printf '  %s8%s  All commands (help)\n'                                                     "$C_B" "$C_0"
    printf '  %sq%s  Quit\n'                                                                    "$C_B" "$C_0"
    printf '\nChoose [1-8, q]: '
    local choice; IFS= read -r choice || { printf '\n'; return 0; }
    case "$choice" in
      1) cmd_console ;;                              # foreground; execs docker run (replaces process)
      2) exec "$ROOT/local_deploy.sh" ;;
      3) exec "$ROOT/remote_deploy.sh" ;;
      4) ( cmd_setup ) || warn "setup did not complete" ;;
      5) ( cmd_serve ) || warn "serve did not complete" ;;
      6) ( cmd_stop )  || warn "stop did not complete" ;;
      7) ( cmd_status; printf '\n'; cmd_doctor ) || true ;;
      8) usage; return 0 ;;
      q|Q|"") return 0 ;;
      *) warn "invalid choice: $choice"; continue ;;
    esac
    # Non-terminal actions return here; pause so the output is readable, then redraw.
    case "$choice" in
      4|5|6|7) printf '\n%sPress Enter to return to the menu…%s' "$C_B" "$C_0"; IFS= read -r _ || return 0 ;;
    esac
  done
}

usage() {
  cat <<EOF
${C_B}docmcp.sh${C_0} — Documentation MCP Server helper (Docker-based; only Docker is required)

  ${C_B}(no args)${C_0}                 interactive menu (console · deploy · setup · ops)

  ${C_B}setup${C_0}                     build the image, create .env + tokens.json (admin token)
  ${C_B}add${C_0} <path>...             stage files/dirs into raw/
  ${C_B}ingest${C_0} [--full]           build the searchable store from raw/ (in a container)
  ${C_B}serve${C_0}                     start the server + reverse proxy (background)
  ${C_B}console${C_0} [--port N] [--build]  launch the admin/setup web console (loopback only)
  ${C_B}test${C_0} [token]              exercise the running server (list/read)
  ${C_B}status${C_0}                    show services, URL, and index summary
  ${C_B}inventory${C_0}                 corpus breakdown by type + top-level folder
  ${C_B}doctor${C_0}                    health checks (non-zero exit if unhealthy)
  ${C_B}backup${C_0} [dir]             snapshot tokens.json + groups.json + .env + audit + Caddy certs (→ ./backups)
  ${C_B}token${C_0} <user> <prefix...> [--group <name>] [--write <prefix>] [--expires <Nd|Nh|never>] [--comment <text>] | --all  mint a token (--write = portal upload scope)
  ${C_B}token-list${C_0} [--expired]     show configured tokens (or only expired ones)
  ${C_B}token-rm${C_0} <token|user>     revoke a token (or all of a user's tokens)
  ${C_B}token-rotate${C_0} <user>       mint a fresh token with the same scope; revoke the old
  ${C_B}group${C_0} <name> <prefix...>   define/update an RBAC group; ${C_B}group-list${C_0} · ${C_B}group-rm${C_0} <name>
  ${C_B}access-check${C_0} <user> <path> does the user's scope allow the path? (ALLOW/DENY)
  ${C_B}access-tree${C_0} (alias ${C_B}tree${C_0})   who-can-read/write tree: groups (folders+members) + users
  ${C_B}audit${C_0} [N]                  show the last N token create/revoke/rotate events
  ${C_B}logs${C_0}                      follow the server + proxy logs
  ${C_B}stop${C_0}                      stop services (your ingested store is kept)
  ${C_B}build${C_0} [server|ingest|all] (re)build images after code changes
  ${C_B}models${C_0} [--repair]         verify the vendored Docling models (tree + baked image); --repair re-pulls via Git LFS
  ${C_B}schedule${C_0} <Nm|Nh|daily|off> run 'ingest' on a schedule — cron, or a systemd timer if cron is absent (no arg shows it)

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
# Only dispatch when EXECUTED directly. When this file is `source`d (e.g. by the
# local_deploy.sh / remote_deploy.sh wizards, which reuse the helpers and cmd_* funcs),
# stop here so sourcing just defines functions instead of running a command.
[ "${BASH_SOURCE[0]}" = "$0" ] || return 0
# No argument → the interactive menu (it falls back to the full help on a non-TTY).
cmd="${1:-menu}"; shift || true
case "$cmd" in
  menu|start)         cmd_menu "$@" ;;
  setup)              cmd_setup "$@" ;;
  add)                cmd_add "$@" ;;
  ingest)             cmd_ingest "$@" ;;
  serve|up)           cmd_serve "$@" ;;
  console)            cmd_console "$@" ;;
  env-set)            cmd_env_set "$@" ;;
  stop|down)          cmd_stop "$@" ;;
  logs)               cmd_logs "$@" ;;
  build)              cmd_build "$@" ;;
  models)             cmd_models "$@" ;;
  schedule|cron)      cmd_schedule "$@" ;;
  token)              cmd_token "$@" ;;
  token-list|tokens)  cmd_token_list "$@" ;;
  token-rm|token-remove|revoke) cmd_token_rm "$@" ;;
  token-rotate)       cmd_token_rotate "$@" ;;
  group)              cmd_group "$@" ;;
  group-list|groups)  cmd_group_list "$@" ;;
  group-rm)           cmd_group_rm "$@" ;;
  access-check)       cmd_access_check "$@" ;;
  access-tree|tree)   cmd_access_tree "$@" ;;
  audit)              cmd_audit "$@" ;;
  test)               cmd_test "$@" ;;
  status)             cmd_status "$@" ;;
  inventory)          cmd_inventory "$@" ;;
  doctor)             cmd_doctor "$@" ;;
  backup)             cmd_backup "$@" ;;
  help|-h|--help)     usage ;;
  *)                  warn "unknown command: $cmd"; usage; exit 1 ;;
esac
