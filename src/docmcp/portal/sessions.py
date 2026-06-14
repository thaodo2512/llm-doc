"""Stateless signed-cookie sessions + CSRF for the portal.

Login verifies a pasted bearer token (via the existing JsonFileTokenVerifier), then we
issue an HMAC-signed cookie carrying {user, allowed_prefixes, writable_prefixes, csrf,
exp}. The bearer token itself NEVER goes into the cookie or any log. Cookies are
HttpOnly + SameSite=Strict (+ Secure under TLS). CSRF tokens guard state-changing POSTs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

SESSION_TTL = 43200  # 12 hours


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: dict, secret: str) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    mac = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64e(mac)}"


def verify(cookie: str, secret: str) -> dict | None:
    """Return the payload iff the signature is valid and not expired, else None."""
    try:
        body, sig = cookie.split(".", 1)
        expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64d(sig), expected):
            return None
        data = json.loads(_b64d(body))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    exp = data.get("exp")
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None
    return data


def new_session(
    user: str, allowed_prefixes: list[str], writable_prefixes: list[str], ttl: int = SESSION_TTL
) -> dict:
    return {
        "user": user,
        "allowed_prefixes": list(allowed_prefixes),
        "writable_prefixes": list(writable_prefixes),
        "csrf": secrets.token_urlsafe(24),
        "exp": int(time.time()) + ttl,
    }
