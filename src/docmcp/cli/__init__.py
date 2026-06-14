"""``docmcp`` command-line interface.

A packaged CLI for the data-owning operations that historically lived as inline
Python heredocs inside ``docmcp.sh``:

  * read views — ``token-list``, ``group-list``, ``access-check``, ``access-tree``,
    ``audit`` — reuse ``docmcp.console.reads`` (one "who can read/write what"
    implementation shared with the web console);
  * write verbs — ``token`` (mint), ``token-rm``, ``token-rotate``, ``group``,
    ``group-rm`` — own the scope/TTL POLICY (require an explicit scope; ``--all``
    ⇒ ``/``; reject a bare ``/`` prefix; parse ``--expires`` Nd/Nh/Nm/never) and
    delegate the actual write to :mod:`docmcp.tokenstore`.

Run ``python -m docmcp <command> --help``. ``docmcp.sh`` will be migrated to
delegate to these subcommands (replacing the heredocs) in a follow-up step.

Each command takes the path to ``tokens.json``/``groups.json`` (siblings, matching
the deployment layout). The ``token``/``token-rotate`` verbs print ONLY the bare
token to stdout (notes go to stderr) so a caller capturing stdout gets a usable
token — the same contract the console and ``setup`` rely on.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from .. import tokenstore
from ..config import Settings
from ..console import reads


def _settings_for(tokens_file: Path) -> Settings:
    """A minimal :class:`Settings` whose ``tokens_file`` (and derived ``groups_file``
    sibling) point at ``tokens_file``. The RBAC read views consult only those two
    fields, so every other field here is an inert placeholder — direct dataclass
    construction skips ``Settings.load``'s env parsing/validation on purpose."""
    return Settings(
        doc_root=Path("/srv/docs/curated"),
        docstore_root=Path("/srv/docs"),
        source_dirs=[],
        bind_host="127.0.0.1",
        bind_port=8080,
        tokens_file=tokens_file,
        search_backend="ripgrep",
        fts5_db=Path("/srv/docs/index.sqlite"),
        enable_vector=False,
        qdrant_url="",
        openai_api_key="",
        openai_embed_model="",
        embed_chunk_tokens=512,
        allowed_origins=[],
        allowed_hosts=[],
    )


def _fmt_ts(epoch: float | None) -> str:
    if epoch is None:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def _cmd_token_list(args: argparse.Namespace) -> int:
    rows = reads.list_tokens(_settings_for(Path(args.tokens)))
    if args.user:
        rows = [r for r in rows if r["user"] == args.user]
    if args.expired:
        rows = [r for r in rows if r["expired"]]
    if not rows:
        print("(no tokens)")
        return 0
    for r in rows:
        read = ", ".join(r["read"]) or "—"
        write = ", ".join(r["write"]) or "—"
        groups = (" groups=[" + ", ".join(r["groups"]) + "]") if r["groups"] else ""
        comment = f"  # {r['comment']}" if r.get("comment") else ""
        flag = " [EXPIRED]" if r["expired"] else ""
        print(
            f"{r['id']}  user={r['user']}  read=[{read}]  write=[{write}]{groups}  "
            f"expires={_fmt_ts(r['expires_at'])}{flag}{comment}"
        )
    return 0


def _cmd_group_list(args: argparse.Namespace) -> int:
    groups = reads.list_groups(_settings_for(Path(args.tokens)))
    if not groups:
        print("(no groups)")
        return 0
    for g in groups:
        prefixes = ", ".join(g["prefixes"]) or "—"
        members = ", ".join(g["members"]) or "—"
        print(f"{g['name']}: prefixes=[{prefixes}]  members=[{members}]")
    return 0


def _cmd_access_check(args: argparse.Namespace) -> int:
    res = reads.access_check(_settings_for(Path(args.tokens)), args.user, args.path)
    if res["result"] == "UNKNOWN":
        print(f"UNKNOWN  user={args.user} has no tokens")
        return 2
    print(f"{res['result']}  user={args.user}  path={args.path}  (effective: {res['scope']})")
    return 0 if res["result"] == "ALLOW" else 1


def _cmd_access_tree(args: argparse.Namespace) -> int:
    tree = reads.access_tree(_settings_for(Path(args.tokens)))
    print("GROUPS:")
    if not tree["groups"]:
        print("  (none)")
    for g in tree["groups"]:
        print(
            f"  {g['name']}: [{', '.join(g['prefixes']) or '—'}]  "
            f"members: {', '.join(g['members']) or '—'}"
        )
    print("USERS:")
    if not tree["users"]:
        print("  (none)")
    for u in tree["users"]:
        print(
            f"  {u['user']}: read=[{', '.join(u['read']) or '—'}]  "
            f"write=[{', '.join(u['write']) or '—'}]  ({u['tokens']} token(s))"
        )
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    rows = reads.audit_tail(Path(args.log), args.n)
    if not rows:
        print("(no audit log yet)")
        return 0
    for rec in rows:
        print(json.dumps(rec, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# write verbs — policy lives here (one tested place); the write lands in tokenstore.

_TTL_UNITS = {"d": 86400, "h": 3600, "m": 60}


def _parse_ttl(spec: str) -> int | None:
    """``Nd``/``Nh``/``Nm`` → seconds; ``never``/``none``/``0``/empty → None (non-expiring).
    Raises :class:`ValueError` on a malformed spec (mirrors cmd_token's validation)."""
    s = (spec or "").strip().lower()
    if s in ("", "never", "none", "0"):
        return None
    unit = _TTL_UNITS.get(s[-1:])
    body = s[:-1]
    if unit is None or not body.isdigit() or not (1 <= int(body) <= 36500):
        raise ValueError(f"invalid --expires {spec!r} (use a positive Nd | Nh | Nm up to 36500, or never)")
    return int(body) * unit


def _resolve_scope(prefixes: list[str], groups: list[str], writes: list[str], grant_all: bool) -> list[str]:
    """Apply the scope POLICY and return the effective read prefixes. ``--all`` ⇒ ``["/"]``
    (alone, not with prefixes/--group); otherwise require an explicit scope and reject empty
    or bare-``/`` read prefixes. Raises :class:`ValueError` on a policy violation."""
    if grant_all:
        if prefixes or groups:
            raise ValueError("use --all alone (not with prefixes/--group)")
        return ["/"]
    if not prefixes and not groups and not writes:
        raise ValueError("specify a read prefix (e.g. /public), --group, --write, or --all — a scope is required")
    for p in prefixes:
        if not p.strip():
            raise ValueError("empty prefix not allowed — pass a real path like /public (or --all)")
        if not p.strip().strip("/"):
            raise ValueError("a bare '/' grants the WHOLE corpus — use --all to do that explicitly")
    return prefixes


def _cmd_token(args: argparse.Namespace) -> int:
    groups = list(args.group or [])
    writes = list(args.write or [])
    prefixes = list(args.prefixes or [])
    spec = args.expires if args.expires is not None else os.environ.get("TOKEN_TTL", "90d")
    try:
        ttl = _parse_ttl(spec)
        prefixes = _resolve_scope(prefixes, groups, writes, args.all)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    created_by = args.by or os.environ.get("TOKEN_BY") or "operator"
    token = tokenstore.mint(
        args.tokens,
        args.user,
        prefixes=prefixes,
        groups=groups,
        writes=writes,
        ttl_seconds=ttl,
        comment=args.comment or "",
        created_by=created_by,
    )
    print(token)  # ONLY the bare token on stdout — callers (console/setup) capture it
    print(f"expires in {spec}" if ttl else "non-expiring token", file=sys.stderr)
    return 0


def _cmd_token_rm(args: argparse.Namespace) -> int:
    removed = tokenstore.revoke(args.tokens, args.target)
    if not removed:
        print(f"error: no token or user matching {args.target!r} (see: token-list)", file=sys.stderr)
        return 1
    for tok in removed:
        print(reads._mask_token(tok))  # never echo a full secret to stdout
    print(f"revoked {len(removed)} token(s)", file=sys.stderr)
    return 0


def _cmd_token_rotate(args: argparse.Namespace) -> int:
    created_by = args.by or os.environ.get("TOKEN_BY") or "operator"
    try:
        token = tokenstore.rotate(args.tokens, args.user, created_by=created_by)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(token)  # ONLY the bare token on stdout
    print(f"rotated {args.user}: new token minted, previous token(s) revoked", file=sys.stderr)
    return 0


def _cmd_group(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.name):
        print("error: group name must match [A-Za-z0-9_-]+", file=sys.stderr)
        return 2
    for p in args.prefixes:
        if not p.strip():
            print("error: empty prefix not allowed", file=sys.stderr)
            return 2
        if not p.strip().strip("/"):
            print("error: a bare '/' grants the WHOLE corpus — a group cannot hold it", file=sys.stderr)
            return 2
    tokenstore.define_group(args.groups, args.name, args.prefixes)
    print(f"group {args.name} = {args.prefixes}")
    return 0


def _cmd_group_rm(args: argparse.Namespace) -> int:
    if tokenstore.remove_group(args.groups, args.name):
        print(f"removed group {args.name}")
    else:
        print(f"no such group: {args.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docmcp", description="docmcp data CLI — token/group mint + RBAC views."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("token-list", help="list tokens (masked) with their effective read/write scope")
    p.add_argument("tokens", help="path to tokens.json (groups.json is read as its sibling)")
    p.add_argument("--user", help="only show this user's tokens")
    p.add_argument("--expired", action="store_true", help="only show expired tokens")
    p.set_defaults(func=_cmd_token_list)

    p = sub.add_parser("group-list", help="list RBAC groups with their prefixes and members")
    p.add_argument("tokens", help="path to tokens.json (groups.json is read as its sibling)")
    p.set_defaults(func=_cmd_group_list)

    p = sub.add_parser(
        "access-check", help="ALLOW / DENY / UNKNOWN for a user over a path (exit 0 / 1 / 2)"
    )
    p.add_argument("tokens", help="path to tokens.json (groups.json is read as its sibling)")
    p.add_argument("user")
    p.add_argument("path", help="a logical doc path, e.g. /public/foo.md")
    p.set_defaults(func=_cmd_access_check)

    p = sub.add_parser("access-tree", help="print the whole access model (groups + users)")
    p.add_argument("tokens", help="path to tokens.json (groups.json is read as its sibling)")
    p.set_defaults(func=_cmd_access_tree)

    p = sub.add_parser("audit", help="show the last N token-audit events (JSONL)")
    p.add_argument("log", help="path to var/token-audit.jsonl")
    p.add_argument("-n", type=int, default=20, help="number of records to show (default 20)")
    p.set_defaults(func=_cmd_audit)

    # --- write verbs (mutate tokens.json / groups.json) --- #
    p = sub.add_parser("token", help="mint a scoped bearer token (prints ONLY the token to stdout)")
    p.add_argument("tokens", help="path to tokens.json")
    p.add_argument("user")
    p.add_argument("prefixes", nargs="*", help="read prefixes, e.g. /public /team/a")
    p.add_argument("--expires", help="Nd | Nh | Nm | never (default $TOKEN_TTL or 90d)")
    p.add_argument("--comment", help="free-text note stored on the token")
    p.add_argument("--group", action="append", help="grant a group's prefixes (repeatable)")
    p.add_argument("--write", action="append", help="a WRITE prefix for the portal (repeatable)")
    p.add_argument("--all", "--admin", dest="all", action="store_true", help="whole-corpus token")
    p.add_argument("--by", help="who is minting this (provenance; default $TOKEN_BY or operator)")
    p.set_defaults(func=_cmd_token)

    p = sub.add_parser("token-rm", help="revoke a token (by token string) or all of a user's tokens")
    p.add_argument("tokens", help="path to tokens.json")
    p.add_argument("target", help="a token string or a user name")
    p.set_defaults(func=_cmd_token_rm)

    p = sub.add_parser("token-rotate", help="re-mint a user's token with the same scope; revoke the old")
    p.add_argument("tokens", help="path to tokens.json")
    p.add_argument("user")
    p.add_argument("--by", help="who is rotating (provenance; default $TOKEN_BY or operator)")
    p.set_defaults(func=_cmd_token_rotate)

    p = sub.add_parser("group", help="define/replace an RBAC group's read prefixes")
    p.add_argument("groups", help="path to groups.json")
    p.add_argument("name", help="group name [A-Za-z0-9_-]+")
    p.add_argument("prefixes", nargs="+", help="read prefixes the group grants")
    p.set_defaults(func=_cmd_group)

    p = sub.add_parser("group-rm", help="delete a group")
    p.add_argument("groups", help="path to groups.json")
    p.add_argument("name")
    p.set_defaults(func=_cmd_group_rm)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
