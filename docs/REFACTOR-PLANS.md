# Refactor migration plans

Two structural refactors identified in the pre-publication review (a third — de-vendoring the model
weights — was **considered and declined**; see §2). Each is **planning only** — no code changes yet.
They are independent and can be sequenced in any order.

---

## 1. Extract data-owning logic from `docmcp.sh` into a `docmcp` Python CLI — effort: **L (~3–5 days)**

**Goal.** Move every data-owning verb (token mint/list/rm/rotate, group CRUD, access-check,
access-tree, audit, inventory, doctor/status probes) out of `docmcp.sh`'s inline `python - <<PY`
heredocs into a packaged CLI (`src/docmcp/cli/` + `src/docmcp/tokenstore.py`) that reuses the
existing `rbac`/`auth`/`atomicio`/`config` code. `docmcp.sh` keeps only Docker/lifecycle verbs and
delegates the data verbs by shelling out to the new entry point.

**Current state.** `docmcp.sh` is ~2040 lines, 29 `cmd_*` functions. ~8 heredoc Python blocks each
re-implement read-modify-write of `tokens.json`/`groups.json` with their *own* copies of
`flock(.tokens.lock)`, hand-rolled `mkstemp`+`os.replace`, token-id generation, TTL math, and
`var/token-audit.jsonl` appends (duplicated across `cmd_token`, `cmd_token_rm`, `cmd_token_rotate`,
`cmd_group`, `cmd_group_rm`). The **read** side already exists in Python in
`src/docmcp/console/reads.py` (`list_tokens`, `list_groups`, `access_check`, `access_tree`,
`audit_tail`); the **write** side has no Python module yet. `atomicio.py` already provides
`atomic_write_text` (the heredocs don't use it).

**Hard constraints (do not break these):**
- `console/commands.py` is a frozen security argv-allowlist; `tests/test_console_commands.py`
  asserts *exact* argv (e.g. `[SH, "token", "alice", "/public", "--expires", "90d"]`). The bash
  verb + flag surface must stay byte-identical.
- The console and `cmd_setup` both capture the minted token off **stdout** → the CLI must print
  **only** the bare token to stdout, all notes to stderr (else `setup` writes a poison-pill
  `tokens.json`).
- The on-disk JSON format must stay unchanged (enables clean rollback).

**Steps.**
1. **Categorize** (decision table, no code). *Keep in bash* (Docker/lifecycle/host): setup, add,
   link, ingest, serve, stop, uninstall, logs, build, models, schedule, console, menu, test,
   env-set, backup. *Move to Python* (data-owning): token, token-list, token-rm, token-rotate,
   group(+list/rm), access-check, access-tree, audit, inventory, and the heredocs inside
   doctor/status (the bash wrappers stay; only the `python - <<PY` bodies move).
2. **Skeleton + write core.** Add `src/docmcp/cli/__init__.py` (stdlib argparse subparsers, matching
   `ingest/pipeline.py`; **no new deps**), `src/docmcp/__main__.py`, and `[project.scripts] docmcp`.
   Add `src/docmcp/tokenstore.py` as the single write authority (`mint`/`revoke`/`rotate`/
   `define_group`/`remove_group`), reusing `atomicio.atomic_write_text` + `flock(.tokens.lock)` +
   `auth.effective_*` + `rbac.is_allowed`.
3. **Port READ verbs first** (lowest risk, output-only). Extract the pure helpers out of
   `console/reads.py` into a shared module so both the console and CLI call them (kills the 3rd copy);
   replace each read heredoc in `docmcp.sh` with `… $SERVER_IMAGE python -m docmcp token-list …`.
4. **Port WRITE verbs** (the crux). argparse surface mirrors `cmd_token` exactly; shrink the bash
   verbs to thin wrappers that keep the guard rails (`need_docker`, `warn_unknown_prefixes`) and call
   `reload_auth_services` unchanged. Delete the heredoc Python.
5. **Port doctor/status probes** last (most entangled with Docker): `verify-tokens`, `check-index`,
   `check-fts5`, `index-count`, portal `/healthz` — preserve exact exit codes (doctor uses exit 1 →
   FAIL).
6. **Rewrite test guards.** Convert the ~8 `tests/test_docmcp_sh.py` grep-the-heredoc guards into
   behavioral Python unit tests against `tokenstore`/CLI; keep the genuinely bash-only ones; add a
   new guard that each wrapper delegates (`python -m docmcp …` appears in the body).
7. **Verify console contract** unchanged (`test_console_commands.py` + `test_console_api.py` pass).

**Top risks.** stdout/stderr contract regression (poison-pill `tokens.json`); console argv drift;
`flock`/atomic-write semantics change; container-vs-host execution (the write verbs run inside
`$SERVER_IMAGE` so `tokenstore.py` must import only stdlib + existing modules); exit-code contract in
doctor/access-check.

**Rollback.** Each step is one commit; the on-disk JSON format is byte-compatible old↔new, so
reverting any wrapper commit restores the inline heredoc with no data migration.

---

## 2. Model weights — DECIDED: keep Git LFS (no change)

**Decision (2026-06-14): keep vendoring the weights in Git LFS as today.** The de-vendoring /
download-on-build approach was evaluated and declined — the current LFS setup gives fully
self-contained, **offline, reproducible** builds (`HF_HUB_OFFLINE=1`) with no dependency on
HuggingFace or any external host, which outweighs the clone-size saving for this project. No code,
Dockerfile, `.gitattributes`, or history changes: `models/**` LFS tracking,
`check_lfs_models`/`repair_models`, and the Dockerfile `COPY models …` path all stay. **No history
rewrite needed.**

**The one cost to stay aware of (public repo).** GitHub's LFS free tier is ~1 GiB storage +
1 GiB/month bandwidth; at ~564 MB, roughly **two full clones per month exhaust the free bandwidth**,
after which LFS pulls can fail with quota errors. If the repo gets popular, budget for a GitHub data
pack (~$5/mo per 50 GB) or revisit the Release-asset hybrid then.

**Optional low-effort mitigation (no de-vendoring).** Most contributors — docs, frontend, bash,
Python-logic — never rebuild the ingest/embedding images and don't need the weights materialized.
Document that they can clone *without* the ~530 MB payload via
`GIT_LFS_SKIP_SMUDGE=1 git clone …`, and only run `git lfs pull` (or `./docmcp.sh models --repair`)
when they actually build those images. That cuts most contributors' LFS bandwidth to ~zero while
keeping the offline guarantee for the people who do build.

---

## 3. Reorganize `tests/` into `unit` / `integration` / `shell` — effort: **S (~30–45 min)**

**Goal.** Split the 20 flat `test_*.py` (211 tests) into `tests/unit/` (fast, no torch/qdrant) and
`tests/integration/` (live-server/docling/qdrant), with `tests/shell/` for the bash test. Fast
default: `pytest tests/unit`. Keep the `docling`/`vector` markers and the shared
`conftest.py`/`fixtures/` at the `tests/` root.

**Classification.**
- **unit/ (15):** `auth`, `rbac`, `docstore_traversal`, `groups`, `review_fixes`, `resource_bounds`,
  `search`, `tools`, `ingest`, `reliability`, `console_commands`, `console_runner`, `console_static`,
  `docmcp_sh`, `ingest_pdf` (Docling is fully monkeypatched).
- **integration/ (5):** `smoke` (live HTTP server), `console_api` (live ASGI), `portal` (TestClient),
  `ingest_rich` (`@docling`, real torch), and `test_vector.py` (move the whole file — its 2
  `@vector` cases auto-skip without Qdrant).
- **shell/:** `test_deploy_env.sh` (run via `bash`). `test_docmcp_sh.py` is pytest → stays in unit/.

**Steps.**
1. `mkdir tests/{unit,integration,shell}`. Keep `tests/conftest.py` + `tests/fixtures/` at root, **no
   `__init__.py`** (modules stay rootless; the root conftest reaches both subdirs).
2. `git mv` the 15 unit modules, 5 integration modules, and the shell script into place.
3. **Fix `__file__`-relative paths broken by the +1 depth** (the #1 risk):
   `test_auth.py` `parent`→`parents[1]`; `ingest_rich` `parent`→`parents[1]`; `docmcp_sh`,
   `console_static`, `vector` `parents[1]`→`parents[2]`. `conftest.py` does **not** move → no change.
4. `pyproject.toml`: `testpaths = ["tests/unit", "tests/integration"]` (so bare `pytest` skips the
   `.sh`); keep `asyncio_mode`, `filterwarnings`, and both markers.
5. Document invocations in README: fast `uv run pytest tests/unit`; heavy-no-models
   `uv run pytest -m 'not docling and not vector'`; full `uv run pytest`; shell
   `bash tests/shell/test_deploy_env.sh`.

**Top risks.** Path breakage if step 3 is missed (grep must show zero stale `parent / "fixtures"`
and zero repo-root `parents[1]` in moved files); keep modules rootless to avoid basename collisions.

**Verification.** `pytest --collect-only -q | tail -1` still reports **211**; `pytest tests/unit`
runs with no torch/qdrant installed; `pytest -m 'not docling and not vector'` drops exactly the
1 docling + 2 vector cases.

**Rollback.** Pure `git mv` + 6 tiny edits — one `git revert`.

---

## Sequencing note

- **Models stay in Git LFS (decided, §2)** — no history rewrite, no models pipeline change.
- **Plan 3 (tests) is the cheapest and safest** — good first move; it also makes Plan 1 easier to
  validate (clear fast subset).
- **Plan 1 (bash→Python) is the largest** and the only one touching the security-sensitive token
  path; do it last, incrementally, with `test_console_commands.py` as the guardrail.
