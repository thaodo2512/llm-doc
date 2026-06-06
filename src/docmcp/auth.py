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
import time
from pathlib import Path

from fastmcp.server.auth import AccessToken, TokenVerifier


class JsonFileTokenVerifier(TokenVerifier):
    """Opaque bearer tokens read from `tokens.json`.

    Shape: ``{ "<token>": {"user": str, "allowed_prefixes": [str],
    "expires_at"?: epoch_seconds, "scopes"?: [str]} }``.
    """

    def __init__(self, tokens_file: str | Path):
        super().__init__()
        self._path = Path(tokens_file)
        tokens = self._load()
        # Precompute fixed-width SHA-256 digests so comparison is constant time and
        # type-safe for arbitrary (incl. non-ASCII) input.
        self._digests: list[tuple[bytes, dict]] = [
            (hashlib.sha256(token.encode("utf-8")).digest(), record)
            for token, record in tokens.items()
        ]

    def _load(self) -> dict[str, dict]:
        if not self._path.is_file():
            return {}
        data = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("tokens file must be a JSON object of token -> {user, allowed_prefixes}")
        for index, record in enumerate(data.values()):  # index, not token, to avoid leaking secrets
            if not isinstance(record, dict):
                raise ValueError(f"token entry #{index} must be a JSON object")
            if "user" not in record or "allowed_prefixes" not in record:
                raise ValueError(f"token entry #{index} needs 'user' and 'allowed_prefixes'")
            if not isinstance(record["allowed_prefixes"], list):
                raise ValueError(f"token entry #{index}: 'allowed_prefixes' must be a list")
            expires_at = record.get("expires_at")
            if expires_at is not None and not isinstance(expires_at, (int, float)):
                raise ValueError(f"token entry #{index}: 'expires_at' must be a number or absent")
        return data

    async def verify_token(self, token: str) -> AccessToken | None:
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
                "allowed_prefixes": list(matched["allowed_prefixes"]),
            },
        )
