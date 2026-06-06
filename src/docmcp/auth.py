"""Bearer-token authentication + the caller's RBAC claims.

A `TokenVerifier` whose tokens come from a JSON file mapping an opaque token to
`{user, allowed_prefixes}`. FastMCP strips `Bearer ` and calls `verify_token`;
returning `None` yields HTTP 401. The token is matched with a constant-time
comparison and never logged (brief §7.3 / §12).
"""

from __future__ import annotations

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
        self._tokens = self._load()

    def _load(self) -> dict[str, dict]:
        if not self._path.is_file():
            return {}
        data = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("tokens file must be a JSON object of token -> {user, allowed_prefixes}")
        for record in data.values():
            if "user" not in record or "allowed_prefixes" not in record:
                raise ValueError("each token entry needs 'user' and 'allowed_prefixes'")
        return data

    async def verify_token(self, token: str) -> AccessToken | None:
        # Compare against every known token without early-return, so the work is
        # independent of which (if any) token matches. compare_digest is constant
        # time for equal-length inputs.
        matched: dict | None = None
        for known, record in self._tokens.items():
            try:
                if hmac.compare_digest(known, token):
                    matched = record
            except TypeError:
                return None  # non-ASCII token, etc.
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
