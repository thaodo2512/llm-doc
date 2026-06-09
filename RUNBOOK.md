# Operations Runbook — Documentation MCP Server

Recovery and routine-operations procedures. All commands assume Docker on the host
and the repo checked out (with Git LFS pulled — see the README). Health-gate every
change with `./docmcp.sh doctor` (exits non-zero if unhealthy).

The store is layered so recovery is simple:

```
raw/                 source documents      ── backed up via Git LFS (push)        ── irreplaceable
tokens.json, .env    secrets / config      ── backed up via ./docmcp.sh backup    ── irreplaceable
<docstore volume>/   curated + index + …   ── REBUILDABLE from raw/ (cache)        ── disposable
  ├── curated/        served docs (DOC_ROOT, read-only to the server)
  ├── index.json/.md  catalog
  ├── .manifest.json  incremental build cache (+ per-file status/errors)
  ├── ingest-status.json  last-run summary (read by doctor/status)
  └── .ingest.lock    cross-process ingest lock
```

---

## 1. Rebuild the search index from `raw/`

If the index/store is corrupt, missing, or you suspect drift, rebuild it — it's
derived entirely from `raw/`.

```bash
./docmcp.sh ingest          # incremental: only changed files are re-parsed
# full reprocess (ignore the manifest, rebuild everything):
docker compose -p docs-mcp --profile ingest run --rm ingest --full
./docmcp.sh doctor          # verify: index valid, doc count, last-ingest result
```

Ingest publishes **atomically** (temp + `os.replace`, FTS5 built into a temp DB and
swapped) and is serialized by a cross-process lock, so a reader never sees a
half-built index and two ingests can't race. The curated store is mounted **read
only** by the server — only ingest writes it.

## 2. Recover after a failed ingest

```bash
./docmcp.sh status          # shows last ingest time + failed count
./docmcp.sh doctor          # FAILS if the last ingest had failures or index is bad
```

Per-file failures are recorded, not just logged. Inspect them:

```bash
VOL="$(docker volume ls --format '{{.Name}}' | grep docstore | head -1)"
docker run --rm -v "$VOL:/srv/docs:ro" docs-mcp:server \
  python -c 'import json;print(json.load(open("/srv/docs/ingest-status.json"))["failures"])'
```

A failed source has no curated output and is excluded from the index. While the source
is **unchanged**, it is **not** re-parsed on subsequent runs (so a poison file can't
burn CPU every cycle) — it stays listed as failed. Fix the offending file in `raw/`
(e.g. an un-pulled Git-LFS pointer, a corrupt PDF) and re-run `./docmcp.sh ingest`
(changing the file clears the skip), or **force a retry** without changing it:

```bash
docker compose -p docs-mcp --profile ingest run --rm ingest --retry-failed
```

For deeper diagnosis:

```bash
DOCMCP_INGEST_DEBUG=1 docker compose -p docs-mcp --profile ingest run --rm ingest --full
```

## 3. Revoke a leaked token

```bash
./docmcp.sh token-rm <token-or-user>     # by exact token string OR by user name
```

The write is atomic and the running server **reloads `tokens.json` on its next
request** (mtime-watched) — revocation is live without a restart (the command also
restarts as a belt-and-suspenders). Confirm:

```bash
./docmcp.sh token-list                   # the revoked token is gone
./docmcp.sh token-list --expired         # audit: tokens past their expiry
```

## 4. Rotate a user's tokens

```bash
./docmcp.sh token-rotate alice            # mint a fresh token with alice's SAME scope
                                          # (read prefixes + groups + writable_prefixes
                                          # + comment), revoke the old
```

Prints the new token to hand to the user; records `last_rotated_at`. Portal write access
(`writable_prefixes`) is carried over, so rotating a portal contributor keeps their upload
rights. The old token is
revoked atomically and the server picks it up on the next request. `token` also records
`created_at`/`created_by`/`--comment`, all shown by `token-list`.

## 4b. Manage groups (named prefix sets)

When many teammates share access, grant via **groups** instead of repeating prefixes:

```bash
./docmcp.sh group firmware /team-fw /team-fw-tools   # define/update a group
./docmcp.sh group-list                               # show groups
./docmcp.sh token alice --group firmware             # token inherits the group's prefixes
./docmcp.sh group firmware /team-fw /team-fw-tools /team-fw-archive   # add a folder ONCE
#   ^ every token in the group gains it on the next request (no per-token edits)
./docmcp.sh group-rm firmware                        # delete; members lose those prefixes
```

## 4c. Audit token operations

```bash
./docmcp.sh audit            # last 20 create/revoke/rotate events (JSONL, never the token)
./docmcp.sh audit 100
```

## 5. Verify a teammate's access scope

```bash
./docmcp.sh access-check alice /team-fw/flash.md   # → ALLOW/DENY (exit 0/1), resolves groups
./docmcp.sh token-list                    # see a user's allowed_prefixes
# Prove it end-to-end with their token (list_docs is filtered to their prefixes):
./docmcp.sh test <their-token>            # shows ONLY the docs they can read
```

A `read_doc` outside a caller's prefixes is **denied** (not silently empty); access
decisions are visible in the server's structured access log (`docker compose -p
docs-mcp logs docs-mcp | grep '"denied":true'`).

## 6. Back up

```bash
./docmcp.sh backup                        # → ./backups/docmcp-backup-<ts>.tar.gz (0600)
```

Captures the **irreplaceable** state: `tokens.json`, **`groups.json`** (permission-critical
— group-backed tokens lose access without it), `.env`, the token audit log (`var/`), the
portal audit/version state (`raw/.portal`, gitignored), and the Caddy TLS-data volume
(ACME certs — only on an HTTPS/`DOMAIN` deploy). **Not** included: `raw/` source docs
(back them up by pushing Git LFS) and the curated store/index (rebuildable from `raw/`).

Suggested cadence: `raw/` per commit / daily; `tokens.json` + `groups.json` after every
token/group change; `.env` after every deploy change; the curated store is optional cache.

## 7. Restore from backup

```bash
tar xzf backups/docmcp-backup-<ts>.tar.gz -C /tmp/restore
cp /tmp/restore/tokens.json ./tokens.json && chmod 600 tokens.json
# groups.json is permission-critical — restore it so group-backed tokens keep access:
[ -f /tmp/restore/groups.json ] && cp /tmp/restore/groups.json ./groups.json && chmod 600 groups.json
cp /tmp/restore/.env ./.env && chmod 600 .env
# Token audit + portal state (audit log + kept versions), if present. The `rm -rf`
# makes the portal-state restore idempotent — without it, `cp -R` onto an existing
# raw/.portal would nest as raw/.portal/raw-portal instead of replacing it.
[ -f /tmp/restore/var/token-audit.jsonl ] && mkdir -p var && cp /tmp/restore/var/token-audit.jsonl var/
[ -d /tmp/restore/raw-portal ] && rm -rf raw/.portal && mkdir -p raw && cp -R /tmp/restore/raw-portal raw/.portal
# Caddy certs (HTTPS deploys only):
VOL=docs-mcp_caddy_data; docker volume create "$VOL"
docker run --rm -v "$VOL:/data" -v /tmp/restore:/in docs-mcp:server \
  python -c "import tarfile;tarfile.open('/in/caddy_data.tar.gz').extractall('/')"
# Then restore raw/ from Git LFS and rebuild the store:
git lfs pull && ./docmcp.sh ingest && ./docmcp.sh serve && ./docmcp.sh doctor
```

## 8. Migrate from internal HTTP (VPN) to an HTTPS hostname

The internal-network default serves **plain HTTP by raw IP** (tokens travel
unencrypted — trusted networks only). To move to public/untrusted networks with TLS:

```bash
# in .env:
#   DOMAIN=docs-mcp.company.internal     # Caddy serves automatic HTTPS on :443
#   HTTP_BIND=0.0.0.0
#   ALLOWED_HOSTS=docs-mcp.company.internal,localhost
#   (remove ALLOW_PLAINTEXT_HTTP)
./docmcp.sh serve                         # Caddy obtains a cert via ACME (needs the
                                          # hostname resolvable/reachable, or DNS-01)
./docmcp.sh backup                        # capture the new certs
```

Point clients at `https://docs-mcp.company.internal/mcp`. See the README network
profiles for the loopback-only and raw-IP variants.

## 9. Health check (use everywhere)

```bash
./docmcp.sh doctor      # server up · tokens.json + groups.json valid (real verifier) ·
                        # index valid (+count) · search backend · curated mounted :ro ·
                        # last ingest · portal /healthz (when PORTAL_ENABLED)
```

`doctor` exits non-zero when unhealthy, so it can gate a deploy or a cron step. With
`PORTAL_ENABLED=true` it also fails if the portal container is down or `/healthz` is
unreachable.
