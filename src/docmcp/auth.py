"""Bearer-token authentication + the caller's RBAC claims.

A `TokenVerifier` whose tokens come from a JSON file mapping an opaque token to
`{user, allowed_prefixes}`. FastMCP strips `Bearer ` and calls `verify_token`;
returning `None` yields HTTP 401. The token is matched with a constant-time
comparison and never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

from fastmcp.server.auth import AccessToken, TokenVerifier


class JsonFileTokenVerifier(TokenVerifier):
    """Opaque bearer tokens read from `tokens.json`.

    Shape: ``{ "<token>": {"user": str, "allowed_prefixes": [str],
    "expires_at"?: epoch_seconds, "scopes"?: [str]} }``.
    """

    def __init__(self, tokens_file: str | Path, groups_file: str | Path | None = None):
        super().__init__()
        self._path = Path(tokens_file)
        # Group definitions ({name: [prefixes]}) live in a sibling groups.json by
        # default; a token may reference groups instead of (or alongside) explicit
        # prefixes, so adding a folder to a group grants it to everyone in the group.
        self._groups_path = (
            Path(groups_file) if groups_file is not None else self._path.parent / "groups.json"
        )
        self._groups: dict[str, list[str]] = {}
        # Fixed-width SHA-256 digests so comparison is constant time and type-safe
        # for arbitrary (incl. non-ASCII) input.
        self._digests: list[tuple[bytes, dict]] = []
        self._sig: tuple | None = None  # combined (mtime,size) of tokens.json + groups.json
        self._reload()

    def _stat_sig(self) -> tuple | None:
        # Combined signature of tokens.json AND groups.json so a change to either
        # triggers a reload. tokens.json missing => no usable auth.
        try:
            ts = self._path.stat()
        except OSError:
            return None
        try:
            gs = self._groups_path.stat()
            grp = (gs.st_mtime_ns, gs.st_size)
        except OSError:
            grp = (0, 0)
        return (ts.st_mtime_ns, ts.st_size, grp)  # mtime_ns dodges 1s-granularity misses

    def _reload(self) -> None:
        """(Re)load tokens and recompute digests.

        On a read/parse error, keep the last-known-good set so a transient bad write
        can't lock everyone out — fail-operational, never allow-all (token writes are
        atomic, so a partial read should not happen; this is defense in depth).
        """
        sig = self._stat_sig()
        try:
            tokens = self._load()
            groups = self._load_groups()
        except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
            if self._digests:
                print(f"[auth] keeping previous tokens; reload failed: {exc}", file=sys.stderr)
                return
            raise
        # Build locally then swap the references (atomic in CPython) so a concurrent
        # verify_token never sees a half-rebuilt set.
        self._digests = [
            (hashlib.sha256(token.encode("utf-8")).digest(), record)
            for token, record in tokens.items()
        ]
        self._groups = groups
        self._sig = sig

    def _reload_if_changed(self) -> None:
        if self._stat_sig() != self._sig:
            self._reload()

    def _load(self) -> dict[str, dict]:
        if not self._path.is_file():
            return {}
        text = self._path.read_text(encoding="utf-8").strip()
        if not text:  # a 0-byte / whitespace-only file is empty config, not a crash
            return {}
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("tokens file must be a JSON object of token -> {user, allowed_prefixes}")
        for index, record in enumerate(data.values()):  # index, not token, to avoid leaking secrets
            if not isinstance(record, dict):
                raise ValueError(f"token entry #{index} must be a JSON object")
            if "user" not in record:
                raise ValueError(f"token entry #{index} needs 'user'")
            # A scope may be explicit prefixes and/or group references (resolved at
            # verify time). A token with neither denies-by-default (sees nothing).
            allowed = record.get("allowed_prefixes")
            if allowed is not None and not isinstance(allowed, list):
                raise ValueError(f"token entry #{index}: 'allowed_prefixes' must be a list")
            grp = record.get("groups")
            if grp is not None and not (isinstance(grp, list) and all(isinstance(g, str) for g in grp)):
                raise ValueError(f"token entry #{index}: 'groups' must be a list of strings")
            wp = record.get("writable_prefixes")  # optional portal: WRITE scope (default [])
            if wp is not None and not isinstance(wp, list):
                raise ValueError(f"token entry #{index}: 'writable_prefixes' must be a list")
            expires_at = record.get("expires_at")
            if expires_at is not None and not isinstance(expires_at, (int, float)):
                raise ValueError(f"token entry #{index}: 'expires_at' must be a number or absent")
        return data

    def _load_groups(self) -> dict[str, list[str]]:
        if not self._groups_path.is_file():
            return {}
        text = self._groups_path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("groups file must be a JSON object of name -> [prefixes]")
        for name, prefixes in data.items():
            if not (isinstance(prefixes, list) and all(isinstance(p, str) for p in prefixes)):
                raise ValueError(f"group '{name}' must map to a list of prefix strings")
        return data

    async def verify_token(self, token: str) -> AccessToken | None:
        # Pick up out-of-band token changes (mint / revoke / rotate) without a
        # restart: a cheap stat, full reload only when the file actually changed.
        self._reload_if_changed()
        # Compare against every known token without early-return, so the work is
        # independent of which (if any) token matches.
        token_digest = hashlib.sha256(token.encode("utf-8")).digest()
        matched: dict | None = None
        for known_digest, record in self._digests:
            if hmac.compare_digest(known_digest, token_digest):
                matched = record
        if matched is None:
            return None

        expires_at = matched.get("expires_at")
        if expires_at is not None and expires_at < time.time():
            return None

        return AccessToken(
            token=token,
            client_id=matched["user"],
            scopes=matched.get("scopes", []),
            expires_at=expires_at,
            claims={
                "user": matched["user"],
                "allowed_prefixes": effective_prefixes(matched, self._groups),
                "writable_prefixes": effective_writable_prefixes(matched),
            },
        )


def effective_writable_prefixes(record: dict) -> list[str]:
    """A token's WRITE prefixes for the optional portal — explicit ``writable_prefixes``
    only (no group expansion), default ``[]`` (deny-by-default ⇒ every existing token is
    read-only). isinstance-guarded so a malformed value contributes nothing. An explicit
    ``/`` is allowed (admin break-glass, minted with ``--write /``)."""
    wp = record.get("writable_prefixes")
    if not isinstance(wp, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for p in wp:
        if not isinstance(p, str):
            continue
        norm = p.strip()  # normalize so "  /team " and whitespace variants can't masquerade
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def effective_prefixes(record: dict, groups: dict) -> list[str]:
    """A token's effective read prefixes: its explicit ``allowed_prefixes`` plus the
    prefixes of any ``groups`` it references, de-duplicated (order preserved).

    Robust against malformed input (so a raw consumer like ``access-check`` agrees with
    the validated server): a non-list ``allowed_prefixes`` or group value contributes
    nothing rather than char-splitting (which could yield a stray ``/`` = whole corpus).
    And a **group** can never grant the whole corpus (``""`` / ``/``) — that is what an
    explicit ``--all`` token is for — so such entries are dropped from group expansion.
    Unknown groups contribute nothing (deny-by-default)."""
    out: list[str] = []
    explicit = record.get("allowed_prefixes")
    if isinstance(explicit, list):
        out.extend(p for p in explicit if isinstance(p, str))
    names = record.get("groups")
    if isinstance(names, list):
        for name in names:
            vals = groups.get(name)
            if not isinstance(vals, list):
                continue  # malformed / unknown group → nothing (deny-by-default)
            for p in vals:
                if isinstance(p, str) and p.strip().strip("/") != "":  # never whole-corpus
                    out.append(p)
    seen: set[str] = set()
    result: list[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result
