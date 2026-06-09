#!/usr/bin/env bash
#
# Unit tests for the deploy wizards' .env helpers (env_set/env_unset) and the
# profile->env matrix (profile_local/vpn/https). Pure bash, no Docker, runnable on
# macOS bash 3.2. Run:  bash tests/test_deploy_env.sh
#
# shellcheck disable=SC1090,SC2034  # dynamic .env source + globals read by the sourced lib
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
# shellcheck source=../docmcp.sh
. "$REPO/docmcp.sh"                 # defines helpers; source-guard stops before dispatch
# shellcheck source=../lib/deploy-common.sh
. "$REPO/lib/deploy-common.sh"

fail=0
ok()  { printf 'ok   %s\n' "$1"; }
# eq LABEL ACTUAL EXPECTED
eq()  { if [ "$2" = "$3" ]; then ok "$1"; else printf 'FAIL %s\n     want=[%s]\n     got =[%s]\n' "$1" "$3" "$2"; fail=1; fi; }

mode_of() { stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1"; }
valof()   { local pfx="$1=" l; l="$(grep "^$1=" "$2" | head -n1)"; printf '%s' "${l#"$pfx"}"; }
count()   { grep -c "$1" "$2" 2>/dev/null || true; }

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT

# --- env_set: append + replace ---------------------------------------------
DEP_ENV="$work/e1"; : >"$DEP_ENV"
env_set FOO bar
eq "append new key" "$(valof FOO "$DEP_ENV")" "bar"
env_set FOO baz
eq "replace existing value" "$(valof FOO "$DEP_ENV")" "baz"
eq "replace keeps one line" "$(count '^FOO=' "$DEP_ENV")" "1"

# --- value with sed-special chars round-trips byte-for-byte -----------------
DEP_ENV="$work/e2"; : >"$DEP_ENV"
sv='a/b&c\d=e+f'
env_set KEY "$sv"
eq "special chars round-trip" "$(valof KEY "$DEP_ENV")" "$sv"

# --- env_unset --------------------------------------------------------------
DEP_ENV="$work/e3"; : >"$DEP_ENV"
env_set A 1; env_set B 2
env_unset A
eq "unset removes key" "$(count '^A=' "$DEP_ENV")" "0"
eq "unset keeps siblings" "$(valof B "$DEP_ENV")" "2"
env_unset NOPE                          # absent key must not error
ok "unset of absent key is a no-op"
eq "mode stays 600 after edits" "$(mode_of "$DEP_ENV")" "600"

# --- no trailing newline in the seed ----------------------------------------
DEP_ENV="$work/e5"; printf 'A=1' >"$DEP_ENV"   # NB: no trailing newline
env_set B 2
eq "no-newline seed keeps A" "$(valof A "$DEP_ENV")" "1"
eq "no-newline seed adds B" "$(valof B "$DEP_ENV")" "2"

# --- exact-prefix: HTTP_BIND must not cross-match HTTP_PORT ------------------
DEP_ENV="$work/e6"; : >"$DEP_ENV"
env_set HTTP_BIND 0.0.0.0; env_set HTTP_PORT 80; env_set HTTP_BIND 127.0.0.1
eq "prefix: HTTP_BIND updated" "$(valof HTTP_BIND "$DEP_ENV")" "127.0.0.1"
eq "prefix: HTTP_PORT untouched" "$(valof HTTP_PORT "$DEP_ENV")" "80"
eq "prefix: HTTP_PORT single line" "$(count '^HTTP_PORT=' "$DEP_ENV")" "1"

# --- idempotent: running env_set twice yields an identical file -------------
DEP_ENV="$work/e7"; : >"$DEP_ENV"
env_set FOO bar; cp "$DEP_ENV" "$work/e7.snap"; env_set FOO bar
if cmp -s "$DEP_ENV" "$work/e7.snap"; then ok "env_set is idempotent"; else eq "env_set is idempotent" "changed" "identical"; fi

# --- profile_local: HTTP_BIND/ALLOW_PLAINTEXT_HTTP truly unset --------------
DEP_ENV="$work/e8"; cp "$REPO/.env.example" "$DEP_ENV"
profile_local 8080 0 ""
out="$(set -a; . "$DEP_ENV"; set +a; printf '%s|%s|%s|%s' "${HTTP_BIND+set}" "${ALLOW_PLAINTEXT_HTTP+set}" "${HTTP_PORT:-}" "${ALLOWED_HOSTS:-}")"
eq "local profile (bind/plaintext unset, port, hosts)" "$out" "||8080|localhost,127.0.0.1"

# --- profile_vpn ------------------------------------------------------------
DEP_ENV="$work/e9"; cp "$REPO/.env.example" "$DEP_ENV"
profile_vpn 10.0.0.5 0.0.0.0 80 0 ""
out="$(set -a; . "$DEP_ENV"; set +a; printf '%s|%s|%s|%s' "${DOMAIN+set}" "${HTTP_BIND:-}" "${ALLOW_PLAINTEXT_HTTP:-}" "${ALLOWED_HOSTS:-}")"
eq "vpn profile (domain unset, bind, plaintext, hosts)" "$out" "|0.0.0.0|true|10.0.0.5,localhost,127.0.0.1"

# --- profile_https + portal: ALLOW_PLAINTEXT_PORTAL must stay UNSET ---------
DEP_ENV="$work/e10"; cp "$REPO/.env.example" "$DEP_ENV"
profile_https docs.example.com 1 ""
out="$(set -a; . "$DEP_ENV"; set +a; printf '%s|%s|%s|%s|%s' "${DOMAIN:-}" "${ALLOW_PLAINTEXT_HTTP+set}" "${ALLOWED_HOSTS:-}" "${PORTAL_ENABLED:-}" "${ALLOW_PLAINTEXT_PORTAL+set}")"
eq "https profile (domain, plaintext unset, hosts, portal on, secure cookies)" "$out" "docs.example.com||docs.example.com,localhost|true|"

# --- re-run with portal+vector turned OFF must CLEAR them (idempotent convergence) -----
DEP_ENV="$work/e11"; cp "$REPO/.env.example" "$DEP_ENV"
profile_local 8080 1 "sk-SECRET"        # portal + vector ON
profile_local 8080 0 ""                 # ...then OFF on re-run
out="$(set -a; . "$DEP_ENV"; set +a; printf '%s|%s|%s|%s' "${PORTAL_ENABLED+s}" "${ALLOW_PLAINTEXT_PORTAL+s}" "${ENABLE_VECTOR+s}" "${OPENAI_API_KEY+s}")"
eq "re-run with portal/vector OFF clears all four keys" "$out" "|||"

# --- profile_local with portal=1 sets plaintext portal cookies (loopback is trusted) ---
DEP_ENV="$work/e12"; cp "$REPO/.env.example" "$DEP_ENV"
profile_local 8080 1 ""
out="$(set -a; . "$DEP_ENV"; set +a; printf '%s|%s' "${PORTAL_ENABLED:-}" "${ALLOW_PLAINTEXT_PORTAL:-}")"
eq "local portal=1 -> plaintext portal cookies" "$out" "true|true"

# --- a vkey enables vector + writes the key ---------------------------------
DEP_ENV="$work/e13"; cp "$REPO/.env.example" "$DEP_ENV"
profile_vpn 10.0.0.5 0.0.0.0 80 0 "sk-KEY"
out="$(set -a; . "$DEP_ENV"; set +a; printf '%s|%s' "${ENABLE_VECTOR:-}" "${OPENAI_API_KEY:-}")"
eq "vkey -> ENABLE_VECTOR=true + OPENAI_API_KEY set" "$out" "true|sk-KEY"

# --- profile_https clears a stale HTTP_PORT from a prior VPN/local run -------
DEP_ENV="$work/e14"; : >"$DEP_ENV"
env_set HTTP_PORT 8080                   # stale custom port
profile_https docs.example.com 0 ""
out="$(set -a; . "$DEP_ENV"; set +a; printf '%s' "${HTTP_PORT+s}")"
eq "https clears stale HTTP_PORT (ACME/redirect use :80)" "$out" ""

# --- dep_load_env clears a profile-managed key removed from the file (stale-export fix) -
DEP_ENV="$work/e15"; : >"$DEP_ENV"
profile_local 8080 0 ""                  # file ends up with NO HTTP_BIND
export HTTP_BIND=0.0.0.0                  # simulate cmd_setup's load_env exporting the .env.example default
dep_load_env                             # must UNSET HTTP_BIND because the file lacks it
eq "dep_load_env clears stale HTTP_BIND from the process env" "${HTTP_BIND+s}" ""
unset HTTP_BIND HTTP_PORT ALLOWED_HOSTS 2>/dev/null || true

# --- dep_load_env also clears a stale HTTP_PORT removed by profile_https (ACME fix) ----
DEP_ENV="$work/e16"; : >"$DEP_ENV"
profile_https docs.example.com 0 ""      # file ends up with NO HTTP_PORT
export HTTP_PORT=8080                     # simulate a stale export from a prior VPN run
dep_load_env
eq "dep_load_env clears stale HTTP_PORT from the process env" "${HTTP_PORT+s}" ""
unset HTTP_PORT DOMAIN HTTP_BIND ALLOWED_HOSTS 2>/dev/null || true

# --- _check_flag: empty skips, valid passes, invalid die()s -----------------
if ( _check_flag "" v_port "--p" ) >/dev/null 2>&1; then ok "_check_flag skips an empty value"; else printf 'FAIL _check_flag empty\n'; fail=1; fi
if ( _check_flag "8080" v_port "--p" ) >/dev/null 2>&1; then ok "_check_flag passes a valid value"; else printf 'FAIL _check_flag valid\n'; fail=1; fi
if ( _check_flag "abc" v_port "--p" ) >/dev/null 2>&1; then printf 'FAIL _check_flag should die on invalid\n'; fail=1; else ok "_check_flag dies on an invalid value"; fi

# --- validator matrix -------------------------------------------------------
# vcheck LABEL EXPECT(ok|bad) validator value
vcheck() {
  local label="$1" expect="$2"; shift 2
  local got=bad; if "$@" >/dev/null 2>&1; then got=ok; fi
  if [ "$got" = "$expect" ]; then ok "$label"; else printf 'FAIL %s (expected %s, got %s)\n' "$label" "$expect" "$got"; fail=1; fi
}
vcheck "v_hostname rejects ':8080'" bad v_hostname ":8080"
vcheck "v_hostname accepts docs.example.com" ok v_hostname "docs.example.com"
vcheck "v_hostname rejects scheme/path" bad v_hostname "http://x/y"
vcheck "v_port accepts 8080" ok v_port "8080"
vcheck "v_port accepts 80 (warns <1024)" ok v_port "80"
vcheck "v_port rejects 99999" bad v_port "99999"
vcheck "v_port rejects abc" bad v_port "abc"
vcheck "v_ip accepts 10.0.0.5" ok v_ip "10.0.0.5"
vcheck "v_ip rejects 999.0.0.1 (octet >255)" bad v_ip "999.0.0.1"
vcheck "v_ip rejects junk" bad v_ip "nope"
vcheck "v_cron accepts 30m" ok v_cron "30m"
vcheck "v_cron rejects 90m (>59)" bad v_cron "90m"
vcheck "v_cron rejects 0m" bad v_cron "0m"
vcheck "v_cron accepts 2h" ok v_cron "2h"
vcheck "v_cron rejects 48h (>23)" bad v_cron "48h"
vcheck "v_cron accepts daily" ok v_cron "daily"
vcheck "v_cron accepts a 5-field expr" ok v_cron "*/15 * * * *"

# --- availability pre-flight (is a value FREE/USABLE now, not just well-formed) ----
# rc_of FN ARGS...  -> echoes the function's 3-way return code without tripping set -e
rc_of() { local rc; "$@" >/dev/null 2>&1 && rc=0 || rc=$?; printf '%s' "$rc"; }

# v_path now also requires readability (a real usability check, not just existence)
vcheck "v_path accepts a readable file" ok v_path "$DEP_ENV"
vcheck "v_path rejects a missing path"  bad v_path "$work/no-such-thing-xyz"
vcheck "v_path accepts empty (= skip)"  ok v_path ""

if command -v python3 >/dev/null 2>&1; then
  # Start a REAL listener on an OS-assigned free loopback port; write the port to a file.
  pf="$work/busyport"
  python3 -c 'import socket,sys,time
s=socket.socket(); s.bind(("127.0.0.1",0)); s.listen(1)
open(sys.argv[1],"w").write(str(s.getsockname()[1]))
time.sleep(15)' "$pf" &
  lp=$!
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do [ -s "$pf" ] && break; sleep 0.2; done
  busyport="$(cat "$pf" 2>/dev/null || true)"
  if [ -n "$busyport" ]; then
    eq "port_busy detects a live listener" "$(rc_of port_busy "$busyport")" "0"
    DEP_OWN_PORTS=""
    eq "_port_ok_for_deploy rejects a foreign-held port" "$(rc_of _port_ok_for_deploy "$busyport")" "1"
    DEP_OWN_PORTS="$busyport"
    eq "_port_ok_for_deploy allows our own published port" "$(rc_of _port_ok_for_deploy "$busyport")" "0"
    DEP_OWN_PORTS=""
    sug="$(suggest_free_port "$busyport")"
    if [ "$sug" != "$busyport" ]; then ok "suggest_free_port skips the busy port"
    else printf 'FAIL suggest_free_port returned the busy port\n'; fail=1; fi
  else
    printf 'skip  live-listener tests (could not start a listener)\n'
  fi
  kill "$lp" 2>/dev/null || true; wait "$lp" 2>/dev/null || true
  if [ -n "$busyport" ]; then
    for _ in 1 2 3 4 5; do [ "$(rc_of port_busy "$busyport")" = "0" ] && sleep 0.2 || break; done
    eq "port_busy reports a released port free" "$(rc_of port_busy "$busyport")" "1"
  fi
else
  printf 'skip  port availability tests (no python3)\n'
fi

# ip_is_local: loopback/all-interfaces always local; a TEST-NET-3 addr is not (or unknown)
eq "ip_is_local accepts 127.0.0.1" "$(rc_of ip_is_local 127.0.0.1)" "0"
eq "ip_is_local accepts 0.0.0.0"   "$(rc_of ip_is_local 0.0.0.0)"   "0"
case "$(rc_of ip_is_local 203.0.113.7)" in
  1|2) ok "ip_is_local: non-local IP is rejected/uncertain" ;;
  *)   printf 'FAIL ip_is_local should be 1 or 2 for a non-local IP\n'; fail=1 ;;
esac

# domain_resolves: a name under the reserved .invalid TLD must never resolve (1), or 2 if
# no resolver tool exists. (We avoid asserting a positive — that varies by getent vs dig.)
case "$(rc_of domain_resolves "no-such-host-$$.invalid")" in
  1|2) ok "domain_resolves: .invalid does not resolve" ;;
  *)   printf 'FAIL domain_resolves should be 1 or 2 for a .invalid name\n'; fail=1 ;;
esac

echo
if [ "$fail" = 0 ]; then echo "ALL DEPLOY-ENV TESTS PASSED"; else echo "SOME TESTS FAILED"; exit 1; fi
