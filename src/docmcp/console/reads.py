"""Direct, structured read-layer for the console's GET endpoints.

These reads bypass Docker entirely — they parse ``tokens.json`` / ``groups.json`` /
``.env`` straight off the bind-mounted repo and resolve RBAC with the same
``auth``/``rbac`` code the server enforces. (Docker-backed views — status, doctor,
inventory — shell out to the read-only verbs instead; see ``routes.py``.)

Token strings are masked and secret values are redacted before anything leaves here.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .. import rbac
from ..auth import effective_prefixes, effective_writable_prefixes
from ..config import Settings

# .env keys whose values must never be returned in full.
_SECRET_ENV_KEYS = {"SESSION_SECRET", "OPENAI_API_KEY"}


def _mask_token(token: str) -> str:
    return f"{token[:8]}…{token[-4:]}" if len(token) >= 14 else "…"


def _load_json(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def setup_done(settings: Settings) -> bool:
    """True once at least one token exists — i.e. ``setup`` has minted the admin token."""
    return bool(_load_json(settings.tokens_file))


def is_admin_claims(claims: dict) -> bool:
    """A console admin token must resolve to the whole corpus (the ``--all`` token)."""
    return rbac.is_allowed("/", claims.get("allowed_prefixes") or [])


def list_tokens(settings: Settings) -> list[dict]:
    tokens = _load_json(settings.tokens_file)
    groups = _load_json(settings.groups_file)
    now = time.time()
    out: list[dict] = []
    for tok, rec in tokens.items():
        if not isinstance(rec, dict):
            continue
        exp = rec.get("expires_at")
        out.append(
            {
                "id": _mask_token(tok),
                "user": rec.get("user", "?"),
                "read": effective_prefixes(rec, groups),
                "explicit": [p for p in (rec.get("allowed_prefixes") or []) if isinstance(p, str)],
                "groups": [g for g in (rec.get("groups") or []) if isinstance(g, str)],
                "write": effective_writable_prefixes(rec),
                "expires_at": exp,
                "expired": bool(exp is not None and exp < now),
                "created_at": rec.get("created_at"),
                "created_by": rec.get("created_by"),
                "comment": rec.get("comment"),
            }
        )
    out.sort(key=lambda t: (t["user"], t["created_at"] or 0))
    return out


def list_groups(settings: Settings) -> list[dict]:
    groups = _load_json(settings.groups_file)
    tokens = _load_json(settings.tokens_file)
    out: list[dict] = []
    for name, prefixes in groups.items():
        if not isinstance(prefixes, list):
            continue
        members = sorted(
            {
                rec.get("user", "?")
                for rec in tokens.values()
                if isinstance(rec, dict) and name in (rec.get("groups") or [])
            }
        )
        out.append({"name": name, "prefixes": [p for p in prefixes if isinstance(p, str)], "members": members})
    out.sort(key=lambda g: g["name"])
    return out


def access_check(settings: Settings, user: str, path: str) -> dict:
    """ALLOW / DENY / UNKNOWN for a user's effective read scope over a logical path."""
    tokens = _load_json(settings.tokens_file)
    groups = _load_json(settings.groups_file)
    recs = [r for r in tokens.values() if isinstance(r, dict) and r.get("user") == user]
    if not recs:
        return {"user": user, "path": path, "result": "UNKNOWN"}
    allowed: list[str] = []
    for r in recs:
        allowed.extend(effective_prefixes(r, groups))
    return {
        "user": user,
        "path": path,
        "result": "ALLOW" if rbac.is_allowed(path, allowed) else "DENY",
        "scope": sorted(set(allowed)),
    }


def access_tree(settings: Settings) -> dict:
    """Structured who-can-read/write: groups (folders + members) and users (scope)."""
    return {"groups": list_groups(settings), "users": _users_view(settings)}


def _users_view(settings: Settings) -> list[dict]:
    by_user: dict[str, dict] = {}
    for t in list_tokens(settings):
        u = by_user.setdefault(
            t["user"], {"user": t["user"], "read": set(), "write": set(), "tokens": 0}
        )
        u["read"].update(t["read"])
        u["write"].update(t["write"])
        u["tokens"] += 1
    return [
        {"user": u["user"], "read": sorted(u["read"]), "write": sorted(u["write"]), "tokens": u["tokens"]}
        for u in sorted(by_user.values(), key=lambda x: x["user"])
    ]


def config_view(settings: Settings) -> dict:
    """Resolved settings (secrets redacted) + the raw ``.env`` as key/value rows
    (secret values masked, each flagged whether the console may edit it)."""
    from .commands import EDITABLE_KEYS

    env_rows: list[dict] = []
    env_path = Path(settings.tokens_file).parent / ".env"
    try:
        raw = env_path.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        env_rows.append(
            {
                "key": key,
                "value": "***set***" if key in _SECRET_ENV_KEYS and value else value.strip(),
                "secret": key in _SECRET_ENV_KEYS,
                "editable": key in EDITABLE_KEYS,
            }
        )
    return {
        "settings": settings.redacted(),
        "env": env_rows,
        "editable_keys": sorted(EDITABLE_KEYS),
    }


def audit_tail(path: Path, n: int = 50) -> list[dict]:
    """Last ``n`` JSONL records from an audit log (token-audit / console-audit)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-max(1, min(n, 1000)):]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
