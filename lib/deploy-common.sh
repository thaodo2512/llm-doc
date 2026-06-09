#!/usr/bin/env bash
#
# deploy-common.sh — shared primitives for the local_deploy.sh / remote_deploy.sh
# wizards. NOT executable on its own; `source` it AFTER sourcing docmcp.sh (it relies
# on ROOT, PROJECT, DOCSTORE_VOL, info/warn/die, is_true, is_running, server_image_exists,
# public_url, and the cmd_* functions).
#
# Design notes (must hold on macOS bash 3.2 + BSD userland):
#   - .env is edited with a pure-bash temp-rewrite — NEVER `sed -i` (BSD vs GNU differ)
#     and never `sed` on values (they may contain / & \ = +).
#   - No $()-captured heredocs anywhere here, so the repo's apostrophe-in-heredoc gotcha
#     cannot bite. Prompts are read from /dev/tty so the wizard works inside a pipe.

# The .env file the helpers operate on. dep_init points this at the real .env (normal
# run) or a throwaway temp (--dry-run); tests may set it directly before sourcing callers.
DEP_ENV="${DEP_ENV:-${ROOT:-$PWD}/.env}"

# ---------------------------------------------------------------------------
# .env upsert / remove (idempotent, mode-600 preserving)
# ---------------------------------------------------------------------------

# env_set KEY VALUE — replace the active `KEY=...` line(s) in-place, or append. Collapses
# any duplicates to exactly one line, so running the wizard twice converges.
env_set() {
  local key="$1" value="$2" f="$DEP_ENV" dir tmp
  dir="$(dirname "$f")"
  [ -f "$f" ] || { umask 077; : >"$f"; }
  umask 077
  tmp="$(mktemp "$dir/.env.tmp.XXXXXX")" || die "mktemp failed in $dir"  # /.env.tmp.* is gitignored
  # Generate the rewrite in a subshell (its own set -e aborts on a write error BEFORE the
  # swap) and remove the temp on ANY failure. A RETURN trap would NOT fire on a set -e abort
  # in bash 3.2, so cleanup is explicit. `|| [ -n "$line" ]` keeps a final newline-less line;
  # the case drops the old active KEY= line(s) (exact prefix), keeping comments verbatim.
  if ( set -e
       while IFS= read -r line || [ -n "$line" ]; do
         case "$line" in "$key="*) ;; *) printf '%s\n' "$line" ;; esac
       done <"$f"
       printf '%s=%s\n' "$key" "$value"
     ) >"$tmp"; then
    chmod 600 "$tmp"
    mv -f "$tmp" "$f" || { rm -f "$tmp"; die "failed to publish $f"; }
  else
    rm -f "$tmp"; die "failed to rewrite $f"
  fi
}

# env_unset KEY — remove the active `KEY=...` line(s) so the variable is truly absent
# (a present-but-default-relying profile, e.g. LOCAL, needs HTTP_BIND genuinely unset).
env_unset() {
  local key="$1" f="$DEP_ENV" dir tmp
  [ -f "$f" ] || return 0
  dir="$(dirname "$f")"
  umask 077
  tmp="$(mktemp "$dir/.env.tmp.XXXXXX")" || die "mktemp failed in $dir"  # /.env.tmp.* is gitignored
  if ( set -e
       while IFS= read -r line || [ -n "$line" ]; do
         case "$line" in "$key="*) ;; *) printf '%s\n' "$line" ;; esac
       done <"$f"
     ) >"$tmp"; then
    chmod 600 "$tmp"
    mv -f "$tmp" "$f" || { rm -f "$tmp"; die "failed to publish $f"; }
  else
    rm -f "$tmp"; die "failed to rewrite $f"
  fi
}

# dep_load_env — refresh the process env from the (new) DEP_ENV so public_url AND the
# later cmd_serve / docker-compose see the chosen profile.
dep_load_env() {
  # Unset every profile-managed key FIRST. A profile may REMOVE a key from the file (e.g.
  # HTTP_BIND on the loopback profile), but `set -a; . file` only SETS, never UNSETS — so a
  # value cmd_setup's load_env exported earlier from .env.example (HTTP_BIND=0.0.0.0,
  # ALLOW_PLAINTEXT_HTTP=true) would linger. docker compose + cmd_serve read the PROCESS env
  # at HIGHER precedence than --env-file, so a stale HTTP_BIND=0.0.0.0 would actually publish
  # plaintext OFF loopback on a deploy we call "local". Clearing first keeps file == env.
  unset HTTP_BIND HTTP_PORT HTTPS_PORT ALLOW_PLAINTEXT_HTTP DOMAIN ALLOWED_HOSTS \
        PORTAL_ENABLED ALLOW_PLAINTEXT_PORTAL ENABLE_VECTOR OPENAI_API_KEY
  # shellcheck disable=SC1090  # dynamic path by design (same as docmcp.sh load_env)
  if [ -f "$DEP_ENV" ]; then set -a; . "$DEP_ENV"; set +a; fi
}

# ---------------------------------------------------------------------------
# Prompts (adduser feel) — value echoed on stdout; prompt + errors via /dev/tty / stderr
# ---------------------------------------------------------------------------

# ask "Prompt" "default" [validator_fn]  ->  echoes the chosen value
ask() {
  local prompt="$1" default="${2:-}" validator="${3:-}" reply
  while true; do
    if [ -n "$default" ]; then printf '%s [%s]: ' "$prompt" "$default" >/dev/tty
    else printf '%s: ' "$prompt" >/dev/tty; fi
    IFS= read -r reply </dev/tty || die "no TTY for prompts — pass values via flags (see --help)"
    [ -n "$reply" ] || reply="$default"
    # Empty is "required" ONLY when there is no validator to decide: validators like
    # v_path/v_cron accept empty (= skip), while v_ip/v_hostname reject it (= required).
    if [ -z "$reply" ] && [ -z "$validator" ]; then printf '  a value is required\n' >/dev/tty; continue; fi
    if [ -n "$validator" ] && ! "$validator" "$reply"; then continue; fi
    printf '%s' "$reply"
    return 0
  done
}

# ask_yesno "Question" "Y|N"  ->  exit 0 for yes, 1 for no
ask_yesno() {
  local prompt="$1" default="${2:-Y}" reply hint
  case "$default" in [Yy]*) hint="Y/n" ;; *) hint="y/N" ;; esac
  while true; do
    printf '%s [%s]: ' "$prompt" "$hint" >/dev/tty
    IFS= read -r reply </dev/tty || die "no TTY — pass --yes plus the needed flags"
    [ -n "$reply" ] || reply="$default"
    case "$reply" in
      [Yy]|[Yy][Ee][Ss]) return 0 ;;
      [Nn]|[Nn][Oo]) return 1 ;;
      *) printf '  please answer y or n\n' >/dev/tty ;;
    esac
  done
}

# ask_secret "Prompt"  ->  echoes the typed value (no echo to the terminal)
ask_secret() {
  local prompt="$1" reply
  printf '%s: ' "$prompt" >/dev/tty
  IFS= read -rs reply </dev/tty || die "no TTY — pass --vector-key"
  printf '\n' >/dev/tty
  printf '%s' "$reply"
}

# --- validators (return 0 ok / 1 retry; messages to stderr so they show in $() callers) -
v_nonempty() { [ -n "$1" ] || { warn "cannot be empty"; return 1; }; return 0; }

v_port() {
  case "$1" in ''|*[!0-9]*) warn "port must be a number"; return 1 ;; esac
  if [ "$1" -lt 1 ] || [ "$1" -gt 65535 ]; then warn "port must be 1-65535"; return 1; fi
  [ "$1" -ge 1024 ] || warn "port <1024 may need root / a privileged bind"
  return 0
}

v_hostname() {
  case "$1" in
    :*) warn "use --port / HTTP_PORT for ports, not a ':port' DOMAIN"; return 1 ;;
    */*|*' '*) warn "enter a bare hostname — no scheme, path, or spaces"; return 1 ;;
  esac
  [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || { warn "not a valid hostname"; return 1; }
  return 0
}

v_ip() {
  [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || { warn "enter a dotted IPv4 address"; return 1; }
  local o IFS=.
  for o in $1; do                               # 10#$o forces base-10 (avoids octal on 0NN)
    if [ "$((10#$o))" -gt 255 ]; then warn "IPv4 octet out of range (0-255): $o"; return 1; fi
  done
  return 0
}

v_path() {
  [ -z "$1" ] && return 0                       # empty = skip ingest
  [ -e "$1" ] || { warn "path not found: $1"; return 1; }
  return 0
}

v_cron() {
  [ -z "$1" ] && return 0                        # empty = no schedule
  case "$1" in
    off|hourly|daily|weekly) return 0 ;;
    # Mirror cmd_schedule's ranges (minutes 1-59, hours 1-23) so a value the prompt
    # accepts can't die later inside cmd_schedule after the deploy is already live.
    *m) case "${1%m}" in ''|*[!0-9]*) warn "minutes must be a number"; return 1 ;; esac
        if [ "$((10#${1%m}))" -ge 1 ] && [ "$((10#${1%m}))" -le 59 ]; then return 0; fi
        warn "minutes must be 1-59 (got $1)"; return 1 ;;
    *h) case "${1%h}" in ''|*[!0-9]*) warn "hours must be a number"; return 1 ;; esac
        if [ "$((10#${1%h}))" -ge 1 ] && [ "$((10#${1%h}))" -le 23 ]; then return 0; fi
        warn "hours must be 1-23 (got $1)"; return 1 ;;
    *) if [ "$(printf '%s' "$1" | awk '{print NF}')" = 5 ]; then return 0; fi
       warn "schedule: Nm|Nh|hourly|daily|weekly|'m h dom mon dow'|off"; return 1 ;;
  esac
}

# _check_flag VALUE validator label — die if a NON-empty flag value fails its validator,
# so bad --flags fail fast (before setup/serve), mirroring the interactive prompt's checks.
_check_flag() {
  if [ -n "$1" ] && ! "$2" "$1"; then die "invalid $3: $1"; fi
}

# ---------------------------------------------------------------------------
# Profile -> .env matrix (the only place that maps a choice to env vars; testable)
# Each satisfies cmd_serve's exposure gates without a die. portal=1 enables the portal;
# vkey non-empty enables vector search.
# ---------------------------------------------------------------------------

profile_local() {  # PORT PORTAL(0/1) VKEY
  local port="$1" portal="$2" vkey="$3"
  env_unset HTTP_BIND            # absent -> cmd_serve defaults to 127.0.0.1 (loopback)
  env_unset ALLOW_PLAINTEXT_HTTP # not needed on loopback
  env_unset DOMAIN               # plaintext on loopback
  env_set HTTP_PORT "$port"
  env_set ALLOWED_HOSTS "localhost,127.0.0.1"
  _profile_portal "$portal" plaintext        # loopback is trusted -> plaintext cookies ok
  _profile_vector "$vkey"
}

profile_vpn() {    # IP BIND PORT PORTAL(0/1) VKEY
  local ip="$1" bind="$2" port="$3" portal="$4" vkey="$5"
  env_unset DOMAIN
  env_set HTTP_BIND "$bind"
  env_set ALLOW_PLAINTEXT_HTTP true
  env_set HTTP_PORT "$port"
  env_set ALLOWED_HOSTS "${ip},localhost,127.0.0.1"
  _profile_portal "$portal" plaintext
  _profile_vector "$vkey"
}

profile_https() {  # DOMAIN PORTAL(0/1) VKEY
  local domain="$1" portal="$2" vkey="$3"
  env_set DOMAIN "$domain"
  env_set HTTP_BIND 0.0.0.0
  env_unset ALLOW_PLAINTEXT_HTTP            # removed: TLS terminates at Caddy
  env_unset HTTP_PORT                       # HTTPS uses 80/443; a stale custom port breaks ACME/redirect
  env_set ALLOWED_HOSTS "${domain},localhost"
  _profile_portal "$portal" secure          # HTTPS -> Secure cookies (ALLOW_PLAINTEXT_PORTAL must stay unset)
  _profile_vector "$vkey"
}

# _profile_portal PORTAL(0/1) MODE(plaintext|secure) — fully determine portal state so a
# re-run that turns the portal OFF actually clears it (idempotent; no stale write-surface).
_profile_portal() {
  if [ "$1" = 1 ]; then
    env_set PORTAL_ENABLED true
    if [ "$2" = plaintext ]; then env_set ALLOW_PLAINTEXT_PORTAL true; else env_unset ALLOW_PLAINTEXT_PORTAL; fi
  else
    env_unset PORTAL_ENABLED
    env_unset ALLOW_PLAINTEXT_PORTAL
  fi
}

# _profile_vector VKEY — enable vector search with the key, or fully clear it when empty
# (so a declined re-run does not keep calling OpenAI with a stale key).
_profile_vector() {
  if [ -n "$1" ]; then env_set ENABLE_VECTOR true; env_set OPENAI_API_KEY "$1"
  else env_unset ENABLE_VECTOR; env_unset OPENAI_API_KEY; fi
}

# ---------------------------------------------------------------------------
# Deploy flow (shared by both wizards). Respects DRY_RUN / ASSUME_YES globals.
# ---------------------------------------------------------------------------

_yn() { if [ "$1" = 1 ]; then printf 'yes'; else printf 'no'; fi; }
dep_log() { printf '%s %s\n' "${C_Y:-}[dry-run]${C_0:-}" "$*"; }

# dep_init — pick the .env target. In --dry-run, write a throwaway copy so the real .env
# is never touched; otherwise operate on the real $ROOT/.env.
dep_init() {
  if [ -n "${DRY_RUN:-}" ]; then
    local seed
    DEP_ENV="$(mktemp "${TMPDIR:-/tmp}/docmcp-dryenv.XXXXXX")" || die "mktemp failed"
    # The throwaway holds a copy of .env (which can carry SESSION_SECRET / OPENAI_API_KEY),
    # so remove it on exit — the "safe" mode must not leave secrets in /tmp.
    trap 'rm -f "$DEP_ENV"' EXIT
    if [ -f "$ROOT/.env" ]; then seed="$ROOT/.env"; else seed="$ROOT/.env.example"; fi
    if [ -f "$seed" ]; then cp "$seed" "$DEP_ENV"; fi
    chmod 600 "$DEP_ENV"
    info "dry-run: throwaway .env at $DEP_ENV (auto-removed on exit; your real .env is untouched)"
  else
    DEP_ENV="$ROOT/.env"
    need_docker
  fi
}

# dep_detect — read-only state probe; sets DEP_HAVE_* / DEP_RUNNING and prints a summary.
dep_detect() {
  DEP_HAVE_ENV=0; DEP_HAVE_IMAGE=0; DEP_HAVE_TOKENS=0; DEP_RUNNING=0
  if [ -f "$ROOT/.env" ]; then DEP_HAVE_ENV=1; fi
  if [ -f "$ROOT/tokens.json" ]; then DEP_HAVE_TOKENS=1; fi
  if [ -z "${DRY_RUN:-}" ]; then
    if server_image_exists; then DEP_HAVE_IMAGE=1; fi
    if is_running docs-mcp || is_running caddy || is_running portal; then DEP_RUNNING=1; fi
  fi
  info "state: .env=$(_yn "$DEP_HAVE_ENV")  image=$(_yn "$DEP_HAVE_IMAGE")  tokens=$(_yn "$DEP_HAVE_TOKENS")  running=$(_yn "$DEP_RUNNING")"
}

# dep_bootstrap — first-time setup (build image, scaffold .env, mint admin + SESSION_SECRET).
dep_bootstrap() {
  if [ "$DEP_HAVE_IMAGE" = 1 ] && [ "$DEP_HAVE_ENV" = 1 ] && [ "$DEP_HAVE_TOKENS" = 1 ]; then
    return 0
  fi
  if [ -n "${DRY_RUN:-}" ]; then dep_log "would run: ./docmcp.sh setup (build image + .env + admin token)"; return 0; fi
  info "First-time setup (building the image + creating .env + an admin token)…"
  cmd_setup
}

# dep_backup_env — snapshot the existing real .env before we mutate it.
dep_backup_env() {
  [ -f "$ROOT/.env" ] || return 0
  local ts; ts="$(date +%Y%m%d-%H%M%S)"
  if [ -n "${DRY_RUN:-}" ]; then dep_log "would back up .env -> .env.bak.$ts"; return 0; fi
  cp "$ROOT/.env" "$ROOT/.env.bak.$ts"; chmod 600 "$ROOT/.env.bak.$ts"
  info "backed up previous .env -> .env.bak.$ts"
}

# dep_ingest PATH — stage + ingest a docs file/dir (no-op on empty path).
dep_ingest() {
  local path="$1"
  [ -n "$path" ] || return 0
  if [ -n "${DRY_RUN:-}" ]; then dep_log "would run: ./docmcp.sh add $path && ./docmcp.sh ingest"; return 0; fi
  info "Staging + ingesting: $path"
  cmd_add "$path"
  cmd_ingest
}

# dep_restart — stop an existing deployment (if running) then serve the new config. The
# stop is required so Caddy re-publishes the new HTTP_BIND/HTTP_PORT/DOMAIN.
dep_restart() {
  if [ -n "${DRY_RUN:-}" ]; then
    dep_log "would run: ./docmcp.sh stop (if a deployment is already running), then ./docmcp.sh serve"
    return 0
  fi
  if [ "$DEP_RUNNING" = 1 ]; then
    if [ -z "${ASSUME_YES:-}" ] && ! ask_yesno "An existing deployment is running. Stop it and start the new config?" "Y"; then
      die "aborted — the existing deployment is left running"
    fi
    info "Stopping the old deployment…"; cmd_stop
  fi
  info "Starting the new deployment…"; cmd_serve
}

# dep_verify — health-gate the new deployment (non-fatal: report, don't abort).
dep_verify() {
  if [ -n "${DRY_RUN:-}" ]; then dep_log "would run: ./docmcp.sh doctor && ./docmcp.sh test"; return 0; fi
  info "Health check…"
  # Run in a subshell so a die()->exit inside (e.g. cmd_test's precondition checks) is
  # downgraded to a catchable status — this step must REPORT, never abort the wizard.
  ( cmd_doctor ) || warn "doctor reported issues — inspect with ./docmcp.sh logs"
  ( cmd_test )   || warn "smoke test failed — inspect with ./docmcp.sh logs"
}

# dep_schedule SPEC — optional cron auto-ingest.
dep_schedule() {
  local spec="$1"
  [ -n "$spec" ] || return 0
  case "$spec" in off) return 0 ;; esac
  if [ -n "${DRY_RUN:-}" ]; then dep_log "would run: ./docmcp.sh schedule $spec"; return 0; fi
  # Non-fatal, and in a SUBSHELL: cmd_schedule reports errors with die()->exit (e.g. crontab
  # absent on a minimal server, or a flag value that bypassed v_cron). A plain `|| warn`
  # cannot catch an exit from a sourced function, so the wizard would abort AFTER a live
  # serve; the subshell turns that exit into a catchable status.
  ( cmd_schedule "$spec" ) || warn "could not set the '$spec' schedule — set one later: ./docmcp.sh schedule <spec>"
}

# dep_reset — DANGER: stop + delete tokens and the ingested store (explicit opt-in only).
dep_reset() {
  warn "RESET requested — this STOPS services and DELETES tokens.json + the ingested store."
  if [ -n "${DRY_RUN:-}" ]; then dep_log "would: ./docmcp.sh stop ; docker volume rm ${DOCSTORE_VOL} ; rm tokens.json"; return 0; fi
  # A generic --yes does NOT authorize an irreversible wipe (it conventionally means
  # "accept safe defaults"). Require a typed confirmation, or an explicit --force.
  if [ -z "${FORCE_RESET:-}" ]; then
    printf 'Type "wipe" to DELETE tokens + the ingested store (anything else aborts): ' >/dev/tty
    local ans=""
    IFS= read -r ans </dev/tty || die "reset aborted (no TTY; pass --force to wipe non-interactively)"
    [ "$ans" = wipe ] || die "reset aborted (you did not type 'wipe')"
  fi
  cmd_stop || true
  docker volume rm "$DOCSTORE_VOL" >/dev/null 2>&1 || true
  rm -f "$ROOT/tokens.json"
  DEP_HAVE_TOKENS=0
  info "reset complete — a fresh admin token will be minted during setup"
}

# dep_summary PROFILE_LABEL [URL_OVERRIDE] — final report: URL, portal, token, Codex wiring.
dep_summary() {
  local label="$1" url="${2:-}" portal_url tok
  dep_load_env                       # so public_url sees the new DOMAIN/HTTP_PORT
  [ -n "$url" ] || url="$(public_url)"
  printf '\n'
  info "${C_B:-}Deployment ready${C_0:-}  —  profile: $label"
  info "  MCP URL : ${C_B:-}${url}${C_0:-}"
  if is_true "${PORTAL_ENABLED:-false}"; then
    portal_url="$(printf '%s' "$url" | sed 's,/mcp$,/portal,')"
    info "  Portal  : ${C_B:-}${portal_url}${C_0:-}"
  fi
  if [ -z "${DRY_RUN:-}" ] && [ -f "$ROOT/tokens.json" ]; then
    tok="$(grep -oE '"tok_[^"]+"' "$ROOT/tokens.json" | head -n1 | tr -d '"' || true)"
    if [ -n "$tok" ]; then
      local shown="…"   # never print a short/malformed token verbatim
      [ "${#tok}" -gt 14 ] && shown="${tok:0:8}…${tok: -4}"
      info "  Token   : ${shown} (redacted; full: ./docmcp.sh token-list · mint scoped: ./docmcp.sh token <user> /prefix)"
    fi
  fi
  printf '\nPoint OpenAI Codex at it:\n'
  printf '  export DOCS_MCP_TOKEN=<your-tok_...-token>\n'
  printf '  codex mcp add docs --url %s --bearer-token-env-var DOCS_MCP_TOKEN\n' "$url"
  printf '  codex            # then run /mcp inside Codex to confirm it connected\n'
  printf '\nManage:  ./docmcp.sh status  ·  logs  ·  stop\n'
}
