#!/usr/bin/env bash
#
# docmcp.sh — helper for the Documentation MCP Server.
# Linux/macOS compatible (bash 3.2+). Run `./docmcp.sh help` for usage.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"
PYBIN="$VENV/bin/python"

# --- pretty output ----------------------------------------------------------
if [ -t 1 ]; then C_B=$'\033[1m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_0=$'\033[0m'
else C_B=; C_G=; C_Y=; C_R=; C_0=; fi
info() { printf "%s\n" "${C_G}==>${C_0} $*"; }
warn() { printf "%s\n" "${C_Y}warning:${C_0} $*" >&2; }
die()  { printf "%s\n" "${C_R}error:${C_0} $*" >&2; exit 1; }

# Load .env (simple KEY=VALUE lines) so we know paths/ports for our own use.
load_env() {
  if [ -f "$ROOT/.env" ]; then
    set -a; . "$ROOT/.env"; set +a
  fi
}
need_venv() { [ -x "$PYBIN" ] || die "no virtualenv — run: ./docmcp.sh setup"; }

# Host to connect to when testing (0.0.0.0 -> 127.0.0.1).
test_host() {
  local h="${BIND_HOST:-127.0.0.1}"
  [ "$h" = "0.0.0.0" ] && h="127.0.0.1"
  printf "%s" "$h"
}
base_url() { printf "http://%s:%s/mcp" "$(test_host)" "${BIND_PORT:-8080}"; }

# --- commands ---------------------------------------------------------------

cmd_setup() {
  info "Setting up the Python environment (3.11+)"
  if command -v uv >/dev/null 2>&1; then
    [ -d "$VENV" ] || uv venv --python 3.11
    uv pip install -e ".[parse]"            # base + ingestion (Docling/tree-sitter)
  else
    warn "uv not found; falling back to python3 -m venv + pip"
    [ -d "$VENV" ] || python3 -m venv "$VENV"
    "$PYBIN" -m pip install --upgrade pip >/dev/null
    "$PYBIN" -m pip install -e ".[parse]"
  fi

  command -v rg >/dev/null 2>&1 || warn "ripgrep ('rg') not found — install it (Debian/Ubuntu: sudo apt-get install -y ripgrep; Fedora: sudo dnf install ripgrep; macOS: brew install ripgrep)"

  # Git LFS for the raw/ binary corpus (pdf/office/images) — see .gitattributes.
  if git rev-parse --git-dir >/dev/null 2>&1; then
    if git lfs version >/dev/null 2>&1; then
      git lfs install --local >/dev/null 2>&1 || true
      info "git-lfs enabled for this repo (raw/ binaries tracked via LFS)"
    else
      warn "git-lfs not found — raw/ binary docs won't use LFS. Install (Debian/Ubuntu: sudo apt-get install -y git-lfs; Fedora: sudo dnf install git-lfs; macOS: brew install git-lfs) then: git lfs install --local"
    fi
  fi

  [ -f "$ROOT/.env" ]        || { cp "$ROOT/.env.example" "$ROOT/.env"; info "created .env (edit paths as needed)"; }
  mkdir -p "$ROOT/raw"
  if [ ! -f "$ROOT/tokens.json" ]; then
    load_env
    cmd_token admin / >/dev/null
    info "created tokens.json with an 'admin' token (full access) — see: ./docmcp.sh token-list"
  fi
  info "Setup complete. Next: put docs in raw/ then ${C_B}./docmcp.sh ingest${C_0}"
}

cmd_add() {
  [ "$#" -ge 1 ] || die "usage: ./docmcp.sh add <file-or-dir> [<file-or-dir> ...]"
  mkdir -p "$ROOT/raw"
  for src in "$@"; do
    [ -e "$src" ] || { warn "skip (not found): $src"; continue; }
    cp -R "$src" "$ROOT/raw/"
    info "added $src -> raw/"
  done
  info "Now run: ./docmcp.sh ingest  (and 'git add raw/ && git commit' to version them — binaries via LFS)"
}

cmd_ingest() {
  need_venv
  info "Ingesting raw/ -> curated doc store + index"
  "$VENV/bin/docmcp-ingest" "$@"            # pass through e.g. --full
}

cmd_serve() {
  need_venv
  load_env
  info "Serving MCP on $(base_url)  (Ctrl-C to stop)"
  exec "$VENV/bin/docmcp-server"
}

# token <user> [<prefix> ...]   -> mints a token, adds it to tokens.json, prints it
cmd_token() {
  [ "$#" -ge 1 ] || die "usage: ./docmcp.sh token <user> [<allowed-prefix> ...]   (default prefix: /)"
  need_venv
  load_env
  local tokfile="${TOKENS_FILE:-$ROOT/tokens.json}"
  local user="$1"; shift
  "$PYBIN" - "$tokfile" "$user" "$@" <<'PY'
import json, os, secrets, sys
path, user = sys.argv[1], sys.argv[2]
prefixes = sys.argv[3:] or ["/"]
data = json.load(open(path)) if os.path.exists(path) else {}
tok = "tok_%s_%s" % (user, secrets.token_hex(12))
data[tok] = {"user": user, "allowed_prefixes": prefixes}
with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
print(tok)
PY
  warn "restart the server for new tokens to take effect"
}

cmd_token_list() {
  need_venv; load_env
  local tokfile="${TOKENS_FILE:-$ROOT/tokens.json}"
  [ -f "$tokfile" ] || die "no tokens file at $tokfile (run: ./docmcp.sh setup)"
  "$PYBIN" - "$tokfile" <<'PY'
import json, sys
for tok, rec in json.load(open(sys.argv[1])).items():
    print(f"  {tok}\t{rec['user']}\t{rec['allowed_prefixes']}")
PY
}

# test [<token>]   -> exercises the running server (defaults to the first token)
cmd_test() {
  need_venv; load_env
  local tokfile="${TOKENS_FILE:-$ROOT/tokens.json}"
  local token="${1:-}"
  if [ -z "$token" ]; then
    token="$("$PYBIN" -c "import json,sys; d=json.load(open(sys.argv[1])); print(next(iter(d)))" "$tokfile")" \
      || die "no token given and none in $tokfile"
  fi
  info "Testing $(base_url) with token ${token%%_*}_…"
  "$PYBIN" - "$(base_url)" "$token" <<'PY'
import asyncio, sys
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
url, token = sys.argv[1], sys.argv[2]
async def main():
    tr = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"})
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

cmd_status() {
  load_env
  echo "${C_B}root      ${C_0} $ROOT"
  echo "${C_B}venv      ${C_0} $([ -x "$PYBIN" ] && echo present || echo MISSING)"
  echo "${C_B}ripgrep   ${C_0} $(command -v rg >/dev/null 2>&1 && rg --version | head -1 || echo MISSING)"
  echo "${C_B}url       ${C_0} $(base_url)"
  echo "${C_B}DOC_ROOT  ${C_0} ${DOC_ROOT:-?}"
  echo "${C_B}SOURCE    ${C_0} ${SOURCE_DIRS:-?}"
  echo "${C_B}backend   ${C_0} ${SEARCH_BACKEND:-ripgrep}   vector=${ENABLE_VECTOR:-false}"
  local idx="${DOC_ROOT:-./var/curated}/index.json"
  if [ -f "$idx" ] && [ -x "$PYBIN" ]; then
    echo "${C_B}indexed   ${C_0} $("$PYBIN" -c "import json,sys;print(len(json.load(open(sys.argv[1]))),'docs')" "$idx")"
  else
    echo "${C_B}indexed   ${C_0} (not built — run: ./docmcp.sh ingest)"
  fi
}

usage() {
  cat <<EOF
${C_B}docmcp.sh${C_0} — Documentation MCP Server helper

  ${C_B}setup${C_0}                     create venv, install deps, .env + tokens.json
  ${C_B}add${C_0} <path>...             copy files/dirs into raw/
  ${C_B}ingest${C_0} [--full]           build the curated doc store + index from raw/
  ${C_B}serve${C_0}                     run the MCP server (foreground)
  ${C_B}token${C_0} <user> [prefix...]  mint a bearer token (default prefix: /)
  ${C_B}token-list${C_0}                list configured tokens
  ${C_B}test${C_0} [token]              exercise the running server (list/read)
  ${C_B}status${C_0}                    show config + index summary

Typical first run:
  ./docmcp.sh setup
  ./docmcp.sh add /path/to/your/docs
  ./docmcp.sh ingest --full
  ./docmcp.sh serve          # in one terminal
  ./docmcp.sh test           # in another

Connect Codex: point ~/.codex/config.toml at $(base_url 2>/dev/null || echo http://localhost:8080/mcp)
with a bearer token (see clients/codex-config.example.toml).
EOF
}

# --- dispatch ---------------------------------------------------------------
cmd="${1:-help}"; shift || true
case "$cmd" in
  setup)              cmd_setup "$@" ;;
  add)                cmd_add "$@" ;;
  ingest)             cmd_ingest "$@" ;;
  serve)              cmd_serve "$@" ;;
  token)              cmd_token "$@" ;;
  token-list|tokens)  cmd_token_list "$@" ;;
  test)               cmd_test "$@" ;;
  status)             cmd_status "$@" ;;
  help|-h|--help)     usage ;;
  *)                  warn "unknown command: $cmd"; usage; exit 1 ;;
esac
