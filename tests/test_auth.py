"""Bearer-token verification: valid / invalid / expired (brief §10)."""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

import pytest

from docmcp.auth import JsonFileTokenVerifier

TOKENS = Path(__file__).parent / "fixtures" / "tokens.json"


@pytest.fixture
def verifier() -> JsonFileTokenVerifier:
    return JsonFileTokenVerifier(TOKENS)


async def test_valid_token_carries_claims(verifier):
    token = await verifier.verify_token("tok_alice_full")
    assert token is not None
    assert token.client_id == "alice"
    assert token.claims["user"] == "alice"
    assert token.claims["allowed_prefixes"] == ["/"]


async def test_scoped_token(verifier):
    token = await verifier.verify_token("tok_bob_public")
    assert token.claims["allowed_prefixes"] == ["/public"]


async def test_invalid_token_returns_none(verifier):
    assert await verifier.verify_token("tok_does_not_exist") is None


async def test_empty_token_returns_none(verifier):
    assert await verifier.verify_token("") is None


async def test_expired_token_rejected(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(
        json.dumps(
            {"tok_exp": {"user": "x", "allowed_prefixes": ["/"], "expires_at": time.time() - 10}}
        )
    )
    verifier = JsonFileTokenVerifier(path)
    assert await verifier.verify_token("tok_exp") is None


def test_uses_constant_time_comparison():
    # Guards against a regression to plain `==` / dict membership (timing attack).
    src = inspect.getsource(JsonFileTokenVerifier.verify_token)
    assert "compare_digest" in src
