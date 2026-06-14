#!/usr/bin/env bash
#
# local_deploy.sh — interactive wizard to run the Documentation MCP Server on THIS
# machine (Codex + MCP on the same laptop): loopback only, plain HTTP on 127.0.0.1.
# It suggests the right .env, then runs setup/ingest/serve end-to-end. Re-runnable:
# detects an existing deployment and (after confirming) stops the old, starts the new.
#
set -euo pipefail
INVOKE_PWD="$PWD"                                   # remember CWD: sourcing docmcp.sh cd's to repo root
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=docmcp.sh
. "$HERE/docmcp.sh"                                 # reuse helpers + cmd_*; its dispatch is source-guarded
# shellcheck source=lib/deploy-common.sh
. "$HERE/lib/deploy-common.sh"
cd "$INVOKE_PWD"                                    # restore CWD so a relative --docs resolves naturally

usage_local() {
  cat <<EOF
${C_B}local_deploy.sh${C_0} — run the docs MCP on THIS machine (loopback, plain HTTP).

  Interactive by default. Flags pre-seed answers (and enable non-interactive use):
    --port N         host port to publish            (default 8080)
    --docs PATH      stage + ingest this file/dir     (default: skip)
    --portal         enable the upload portal (plaintext on loopback)
    --vector-local   enable semantic search with the OFFLINE local model (no API, no network)
    --vector-key K   enable vector search with this OpenAI API key (legacy online backend;
                     ps-visible on argv; prefer the DOCMCP_OPENAI_API_KEY env var)
    --schedule SPEC  cron auto-ingest (e.g. 30m, daily); 'off'/blank = none
    --yes, -y        accept defaults; skip confirmations (non-interactive)
    --dry-run        show what would happen; writes only a throwaway .env
    --reset          DANGER: stop + wipe tokens and the ingested store first
                     (asks you to type 'wipe'; --yes does NOT authorize it)
    --force          with --reset, wipe non-interactively (no typed confirmation)
    -h, --help
EOF
}

PORT=""; DOCS=""; PORTAL=0; VKEY=""; VEC_LOCAL=""; SCHED=""; ASSUME_YES=""; DRY_RUN=""; RESET=""; FORCE_RESET=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --port)        PORT="${2:-}"; shift 2 || die "--port needs a value" ;;
    --docs)        DOCS="${2:-}"; shift 2 || die "--docs needs a value" ;;
    --portal)      PORTAL=1; shift ;;
    --vector-local) VEC_LOCAL=1; shift ;;
    --vector-key)  VKEY="${2:-}"; shift 2 || die "--vector-key needs a value" ;;
    --schedule)   SCHED="${2:-}"; shift 2 || die "--schedule needs a value" ;;
    --yes|-y)     ASSUME_YES=1; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --reset)      RESET=1; shift ;;
    --force)      FORCE_RESET=1; shift ;;
    -h|--help)    usage_local; exit 0 ;;
    *)            die "unknown option: $1 (see --help)" ;;
  esac
done

# Non-argv way to pass the OpenAI key (avoids ps exposure of --vector-key).
[ -n "$VKEY" ] || VKEY="${DOCMCP_OPENAI_API_KEY:-}"
# --vector-local selects the OFFLINE local embedder (no key). Encoded through the VKEY slot
# as the __local__ sentinel so the profile_* functions need no signature change. Mutually
# exclusive with an OpenAI key.
if [ -n "$VEC_LOCAL" ]; then
  [ -z "$VKEY" ] || die "choose ONE: --vector-local (offline) or --vector-key/OpenAI — not both"
  VKEY="__local__"
fi
# resolve a relative --docs against the user's original CWD (we cd'd back above already)
case "$DOCS" in ""|/*) ;; *) DOCS="$INVOKE_PWD/$DOCS" ;; esac
# Validate any flag-supplied values UP FRONT, before setup/serve, so bad input fails fast.
_check_flag "$PORT" v_port "--port"
_check_flag "$DOCS" v_path "--docs"
_check_flag "$SCHED" v_cron "--schedule"

info "Local deploy — Codex + MCP on this machine (loopback, plain HTTP)."
dep_init
[ -n "$RESET" ] && dep_reset
dep_detect
dep_bootstrap

# --- prompts (skipped when the flag already set the value, or under --yes) ---
PORT_VETTED=""
if [ -z "$PORT" ]; then
  if [ -n "$ASSUME_YES" ]; then PORT=8080
  else PORT="$(ask_free_port "Local port to publish on" "8080")"; PORT_VETTED=1; fi
fi
# A --port flag or the --yes default was never vetted by the interactive free-port loop:
# check it's free now and fail fast with a suggestion (so we don't bind-fail at compose).
[ -n "$PORT_VETTED" ] || require_port_free "$PORT" "port"
if [ -z "$DOCS" ] && [ -z "$ASSUME_YES" ]; then
  DOCS="$(ask "Docs file/dir to ingest now (blank = skip)" "" v_path)"
fi
if [ "$PORTAL" = 0 ] && [ -z "$ASSUME_YES" ]; then
  if ask_yesno "Enable the upload portal (browser uploads)?" "N"; then PORTAL=1; fi
fi
if [ -z "$VKEY" ] && [ -z "$ASSUME_YES" ]; then
  # Default offering is the OFFLINE local model (no API key, nothing leaves the box).
  if ask_yesno "Enable semantic (vector) search with a local offline model (no API key)?" "N"; then
    VKEY="__local__"
  fi
fi
if [ -z "$SCHED" ] && [ -z "$ASSUME_YES" ]; then
  SCHED="$(ask "Auto re-ingest schedule (e.g. 30m, daily; blank = none)" "" v_cron)"
fi

dep_backup_env
profile_local "$PORT" "$PORTAL" "$VKEY"
dep_load_env

dep_ingest "$DOCS"
dep_restart
dep_verify
dep_schedule "$SCHED"
# Match the label to the URL: public_url omits :80, so only show a non-default port.
case "$PORT" in
  80|"") dep_summary "local (loopback)" ;;
  *)     dep_summary "local (loopback, port $PORT)" ;;
esac
