#!/usr/bin/env bash
#
# remote_deploy.sh — interactive wizard to run the Documentation MCP Server on THIS
# (remote) server. Run it ON the server. Choose how clients reach it:
#   - VPN / plaintext (raw IP)  [default] — simplest; bearer tokens are NOT encrypted,
#                                            so use only on a trusted/VPN network.
#   - HTTPS (hostname)                     — Caddy auto-TLS; needs a resolvable, reachable
#                                            DNS name (ports 80/443) or a DNS-01 setup.
# Re-runnable: detects an existing deployment and (after confirming) stops it, starts the new.
#
set -euo pipefail
INVOKE_PWD="$PWD"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=docmcp.sh
. "$HERE/docmcp.sh"
# shellcheck source=lib/deploy-common.sh
. "$HERE/lib/deploy-common.sh"
cd "$INVOKE_PWD"

usage_remote() {
  cat <<EOF
${C_B}remote_deploy.sh${C_0} — run the docs MCP on THIS server. Pick a profile:
    VPN / plaintext (raw IP)  [default]   ·   HTTPS (hostname)

  Interactive by default. Flags pre-seed answers (and pick the profile non-interactively):
    --ip ADDR        VPN profile: the server IP clients use (implies VPN)
    --bind ADDR      host interface to bind            (default 0.0.0.0)
    --port N         HTTP port for the VPN profile     (default 80)
    --domain HOST    HTTPS profile for HOST (Caddy auto-TLS)  (implies HTTPS)
    --docs PATH      stage + ingest this file/dir
    --portal         enable the upload portal
    --vector-key K   enable vector search with this OpenAI API key
                     (ps-visible on argv; prefer the DOCMCP_OPENAI_API_KEY env var)
    --schedule SPEC  cron auto-ingest (e.g. 30m, daily); 'off'/blank = none
    --yes, -y        accept defaults; skip confirmations (needs --ip or --domain)
    --dry-run        show what would happen; writes only a throwaway .env
    --reset          DANGER: stop + wipe tokens and the ingested store first
                     (asks you to type 'wipe'; --yes does NOT authorize it)
    --force          with --reset, wipe non-interactively (no typed confirmation)
    -h, --help
EOF
}

IP=""; BIND=""; PORT=""; DOMAIN_ARG=""; DOCS=""; PORTAL=0; VKEY=""; SCHED=""
ASSUME_YES=""; DRY_RUN=""; RESET=""; FORCE_RESET=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --ip)         IP="${2:-}"; shift 2 || die "--ip needs a value" ;;
    --bind)       BIND="${2:-}"; shift 2 || die "--bind needs a value" ;;
    --port)       PORT="${2:-}"; shift 2 || die "--port needs a value" ;;
    --domain)     DOMAIN_ARG="${2:-}"; shift 2 || die "--domain needs a value" ;;
    --docs)       DOCS="${2:-}"; shift 2 || die "--docs needs a value" ;;
    --portal)     PORTAL=1; shift ;;
    --vector-key) VKEY="${2:-}"; shift 2 || die "--vector-key needs a value" ;;
    --schedule)   SCHED="${2:-}"; shift 2 || die "--schedule needs a value" ;;
    --yes|-y)     ASSUME_YES=1; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --reset)      RESET=1; shift ;;
    --force)      FORCE_RESET=1; shift ;;
    -h|--help)    usage_remote; exit 0 ;;
    *)            die "unknown option: $1 (see --help)" ;;
  esac
done
[ -n "$VKEY" ] || VKEY="${DOCMCP_OPENAI_API_KEY:-}"   # non-argv key (avoids ps exposure)
case "$DOCS" in ""|/*) ;; *) DOCS="$INVOKE_PWD/$DOCS" ;; esac
# Validate any flag-supplied values up front (before profile selection / setup / serve),
# so e.g. a ':port' --domain fails fast here rather than late inside cmd_serve.
_check_flag "$IP" v_ip "--ip"
_check_flag "$BIND" v_ip "--bind"
_check_flag "$PORT" v_port "--port"
_check_flag "$DOMAIN_ARG" v_hostname "--domain"
_check_flag "$DOCS" v_path "--docs"
_check_flag "$SCHED" v_cron "--schedule"

# --- choose the profile (VPN default) ---
[ -n "$IP" ] && [ -n "$DOMAIN_ARG" ] && die "pass --ip (VPN) OR --domain (HTTPS), not both"
PROFILE=""
if [ -n "$DOMAIN_ARG" ]; then PROFILE=https; fi
if [ -n "$IP" ]; then PROFILE=vpn; fi
if [ -z "$PROFILE" ]; then
  if [ -n "$ASSUME_YES" ]; then die "--yes needs --ip (VPN) or --domain (HTTPS) to pick a profile"; fi
  info "How will clients reach this server?"
  info "  1) VPN / plaintext (raw IP)  [default] — simplest; tokens NOT encrypted (trusted/VPN only)"
  info "  2) HTTPS (hostname)                     — Caddy auto-TLS; needs a reachable DNS name"
  case "$(ask "Profile" "1")" in
    1|vpn|VPN)   PROFILE=vpn ;;
    2|https|HTTPS) PROFILE=https ;;
    *)           die "invalid choice — pick 1 or 2" ;;
  esac
fi

info "Remote deploy — profile: $PROFILE"
dep_init
[ -n "$RESET" ] && dep_reset
dep_detect
dep_bootstrap

# --- profile-specific values ---
if [ "$PROFILE" = vpn ]; then
  if [ -z "$IP" ]; then IP="$(ask "This server's IP (clients connect to it)" "" v_ip)"; fi
  warn_ip_unroutable "$IP"                       # soft: flag a typo'd IP (NAT/public is fine)
  BIND_VETTED=""
  if [ -z "$BIND" ]; then
    if [ -n "$ASSUME_YES" ]; then BIND=0.0.0.0
    else BIND="$(ask_bind "Bind interface (0.0.0.0 = all; or pin to the VPN IP)" "0.0.0.0")"; BIND_VETTED=1; fi
  fi
  [ -n "$BIND_VETTED" ] || require_bind_local "$BIND"   # a --bind/--yes value the loop never vetted
  PORT_VETTED=""
  if [ -z "$PORT" ]; then
    if [ -n "$ASSUME_YES" ]; then PORT=80
    else PORT="$(ask_free_port "HTTP port clients will use" "80")"; PORT_VETTED=1; fi
  fi
  [ -n "$PORT_VETTED" ] || require_port_free "$PORT" "port"
  if [ -z "$ASSUME_YES" ]; then
    ask_yesno "Bearer tokens travel UNENCRYPTED on this profile — trusted/VPN network only. Continue?" "Y" \
      || die "aborted — use the HTTPS profile for an untrusted network"
  fi
else  # https
  if [ -z "$DOMAIN_ARG" ]; then DOMAIN_ARG="$(ask "Public hostname for HTTPS (e.g. docs.example.com)" "" v_hostname)"; fi
  if [ -n "$BIND" ] || [ -n "$PORT" ]; then
    warn "--bind/--port are ignored under HTTPS (Caddy serves on 80/443; use HTTPS_PORT in .env to change 443)."
  fi
  preflight_domain "$DOMAIN_ARG"   # checks the name actually resolves; notes 80/443 reachability
fi

# --- common optional prompts ---
if [ -z "$DOCS" ] && [ -z "$ASSUME_YES" ]; then
  DOCS="$(ask "Docs file/dir to ingest now (blank = skip)" "" v_path)"
fi
if [ "$PORTAL" = 0 ] && [ -z "$ASSUME_YES" ]; then
  if ask_yesno "Enable the upload portal (browser uploads)?" "N"; then PORTAL=1; fi
fi
if [ -z "$VKEY" ] && [ -z "$ASSUME_YES" ]; then
  if ask_yesno "Enable vector/semantic search (calls OpenAI)?" "N"; then VKEY="$(ask_secret "OpenAI API key")"; fi
fi
if [ -z "$SCHED" ] && [ -z "$ASSUME_YES" ]; then
  SCHED="$(ask "Auto re-ingest schedule (e.g. 30m, daily; blank = none)" "" v_cron)"
fi

dep_backup_env
if [ "$PROFILE" = vpn ]; then
  profile_vpn "$IP" "$BIND" "$PORT" "$PORTAL" "$VKEY"
else
  profile_https "$DOMAIN_ARG" "$PORTAL" "$VKEY"
fi
dep_load_env

dep_ingest "$DOCS"
dep_restart
dep_verify
dep_schedule "$SCHED"

# Clients on a VPN reach the server by its IP, not localhost; build that URL explicitly.
if [ "$PROFILE" = vpn ]; then
  vpn_url="http://${IP}/mcp"; label_port=""
  case "$PORT" in 80|"") ;; *) vpn_url="http://${IP}:${PORT}/mcp"; label_port=":${PORT}" ;; esac
  dep_summary "remote VPN/plaintext (${IP}${label_port})" "$vpn_url"
else
  dep_summary "remote HTTPS (${DOMAIN_ARG})"
fi
