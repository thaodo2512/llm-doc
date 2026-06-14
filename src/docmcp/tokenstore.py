"""The single write-authority for ``tokens.json`` / ``groups.json``.

Mint / revoke / rotate bearer tokens and define / remove RBAC groups, with the
exact on-disk format, locking, atomic-write, and audit semantics the ``docmcp.sh``
heredocs used — so the bash verbs can delegate here without any behaviour change.
Every mutation:

  * serializes on a sibling ``.tokens.lock`` (``flock`` LOCK_EX) so concurrent
    mint / revoke / rotate / group writes never lose an update — tokens.json and
    groups.json share ONE lock, so token and group writes mutually exclude;
  * publishes atomically (``atomicio.atomic_write_text``, mode 0600) so the
    live-reloading server never reads a half-written file;
  * appends an audit line to ``var/token-audit.jsonl`` (never the token string).

Records match :class:`docmcp.auth.JsonFileTokenVerifier`'s schema:
``{user, allowed_prefixes?, groups?, writable_prefixes?, expires_at?, comment?,
created_at, created_by, last_rotated_at?}``.
"""

from __future__ import annotations

import fcntl
import json
import secrets
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .atomicio import atomic_write_text

_LOCK_NAME = ".tokens.lock"


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """Hold an exclusive ``flock`` on the shared sibling lock for a whole
    read-modify-write. tokens.json and groups.json share this one lock (as the
    heredocs did), so token and group writes mutually exclude."""
    directory.mkdir(parents=True, exist_ok=True)
    lock = open(directory / _LOCK_NAME, "a")  # noqa: SIM115 — held across the with-body
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        lock.close()  # closing releases the flock


def _load(path: Path) -> dict:
    """Load a JSON object, treating missing/empty as ``{}`` (a fresh store)."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a JSON object")
    return data


def _audit(directory: Path, record: dict) -> None:
    """Append one JSONL audit record (best-effort; never raises, never logs a token)."""
    try:
        adir = directory / "var"
        adir.mkdir(parents=True, exist_ok=True)
        with open(adir / "token-audit.jsonl", "a", encoding="utf-8") as af:
            af.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _new_token(user: str) -> str:
    return f"tok_{user}_{secrets.token_hex(12)}"


def mint(
    tokens_file: str | Path,
    user: str,
    *,
    prefixes: Sequence[str] = (),
    groups: Sequence[str] = (),
    writes: Sequence[str] = (),
    ttl_seconds: int | None = None,
    comment: str = "",
    created_by: str = "operator",
) -> str:
    """Mint a bearer token for ``user`` and return it. A scope is REQUIRED — at
    least one of ``prefixes`` / ``groups`` / ``writes`` must be non-empty (the
    caller enforces the policy meaning of each; this is the last-line guard,
    matching the heredoc's ``no scope given`` refusal)."""
    prefixes = list(prefixes)
    groups = list(groups)
    writes = [w.strip() for w in writes if w.strip()]
    if not prefixes and not groups and not writes:
        raise ValueError("a scope is required: pass prefixes, groups, or writes (or --all)")
    path = Path(tokens_file)
    now = int(time.time())
    with _locked(path.parent):
        data = _load(path)
        rec: dict = {"user": user, "created_at": now, "created_by": created_by or "operator"}
        if prefixes:
            rec["allowed_prefixes"] = prefixes
        if groups:
            rec["groups"] = groups
        if writes:
            rec["writable_prefixes"] = writes
        c = (comment or "").strip()
        if c:
            rec["comment"] = c
        if ttl_seconds:
            rec["expires_at"] = now + int(ttl_seconds)
        token = _new_token(user)
        data[token] = rec
        atomic_write_text(path, json.dumps(data, indent=2), mode=0o600)
        _audit(
            path.parent,
            {
                "ts": now,
                "action": "create",
                "user": user,
                "by": rec["created_by"],
                "prefixes": prefixes,
                "groups": groups,
                "writable": writes,
            },
        )
    return token


def revoke(tokens_file: str | Path, target: str) -> list[str]:
    """Revoke by exact token string, or every token belonging to a user. Returns
    the removed token strings (empty list if nothing matched)."""
    path = Path(tokens_file)
    with _locked(path.parent):
        data = _load(path)
        if target in data:
            removed = [target]
        else:
            removed = [t for t, r in data.items() if isinstance(r, dict) and r.get("user") == target]
        if not removed:
            return []
        users = sorted({data[t].get("user", "?") for t in removed if isinstance(data.get(t), dict)})
        for t in removed:
            del data[t]
        atomic_write_text(path, json.dumps(data, indent=2), mode=0o600)
        _audit(
            path.parent,
            {"ts": int(time.time()), "action": "revoke", "users": users, "count": len(removed)},
        )
    return removed


def rotate(tokens_file: str | Path, user: str, *, created_by: str = "operator") -> str:
    """Mint a fresh token carrying the union of the user's existing scope (read
    prefixes + groups + writable prefixes + first comment) and revoke the old
    one(s). Preserves expiry STYLE: the new token expires (90 days) only if EVERY
    old token did. Raises :class:`LookupError` if the user has no tokens."""
    path = Path(tokens_file)
    now = int(time.time())
    with _locked(path.parent):
        data = _load(path)
        old = {t: r for t, r in data.items() if isinstance(r, dict) and r.get("user") == user}
        if not old:
            raise LookupError(f"no tokens for user: {user}")
        prefixes: list[str] = []
        groups: list[str] = []
        writes: list[str] = []
        comment: str | None = None
        for r in old.values():
            for p in r.get("allowed_prefixes") or []:
                if p not in prefixes:
                    prefixes.append(p)
            for g in r.get("groups") or []:
                if g not in groups:
                    groups.append(g)
            for w in r.get("writable_prefixes") or []:
                if w not in writes:
                    writes.append(w)
            if r.get("comment") and not comment:
                comment = r.get("comment")
        rec: dict = {
            "user": user,
            "created_at": now,
            "last_rotated_at": now,
            "created_by": created_by or "operator",
        }
        if prefixes:
            rec["allowed_prefixes"] = prefixes
        if groups:
            rec["groups"] = groups
        if writes:
            rec["writable_prefixes"] = writes
        if comment:
            rec["comment"] = comment
        # Preserve expiry style: only make the new token expiring if EVERY old one was.
        if all(r.get("expires_at") for r in old.values()):
            rec["expires_at"] = now + 90 * 86400
        token = _new_token(user)
        for t in list(old):
            del data[t]
        data[token] = rec
        atomic_write_text(path, json.dumps(data, indent=2), mode=0o600)
        _audit(
            path.parent,
            {"ts": now, "action": "rotate", "user": user, "by": rec["created_by"], "replaced": len(old)},
        )
    return token


def define_group(groups_file: str | Path, name: str, prefixes: Sequence[str]) -> list[str]:
    """Create or replace an RBAC group's prefix list. Returns the stored prefixes."""
    path = Path(groups_file)
    prefixes = list(prefixes)
    with _locked(path.parent):
        data = _load(path)
        data[name] = prefixes
        atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True), mode=0o600)
    return prefixes


def remove_group(groups_file: str | Path, name: str) -> bool:
    """Delete a group. Returns ``False`` if it did not exist (not an error)."""
    path = Path(groups_file)
    with _locked(path.parent):
        data = _load(path)
        if name not in data:
            return False
        del data[name]
        atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True), mode=0o600)
    return True
