"""Tests for daimon.core.mcp_auth.mint_internal_mcp_token.

Sibling to mint_jwt: a Phase 16 v1.1 seam used by `headless_runner` and
(later) Phase 28's /mcp-token mint command. Phase 20 may supersede with a
tenant-aware variant — keep the signature stable.
"""

from __future__ import annotations

import datetime as dt
import uuid

import jwt as pyjwt
import pytest
from daimon.core.mcp_auth import mint_internal_mcp_token


def test_mint_internal_mcp_token_round_trip_decodes_claims() -> None:
    account_id = uuid.uuid4()
    secret = b"a" * 32
    now = dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=dt.UTC)

    token = mint_internal_mcp_token(
        account_id=account_id,
        secret=secret,
        now=now,
    )

    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert decoded == {
        "sub": str(account_id),
        "iat": int(now.timestamp()),
        "is_admin": True,
        "internal": True,
    }, "internal mcp token should carry exactly sub/iat/is_admin/internal (Phase 88-03)"
    assert "platform" not in decoded, "minted token must carry no platform wire claim"
    assert "guild_id" not in decoded, "minted token must carry no guild_id wire claim"


def test_mint_internal_mcp_token_wrong_secret_fails_signature() -> None:
    account_id = uuid.uuid4()
    now = dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=dt.UTC)
    token = mint_internal_mcp_token(
        account_id=account_id,
        secret=b"a" * 32,
        now=now,
    )

    with pytest.raises(pyjwt.InvalidSignatureError):
        pyjwt.decode(token, b"b" * 32, algorithms=["HS256"])


def test_mint_internal_mcp_token_different_accounts_differ() -> None:
    secret = b"a" * 32
    now = dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=dt.UTC)

    token_a = mint_internal_mcp_token(
        account_id=uuid.uuid4(),
        secret=secret,
        now=now,
    )
    token_b = mint_internal_mcp_token(
        account_id=uuid.uuid4(),
        secret=secret,
        now=now,
    )

    assert token_a != token_b, "different account_ids should yield different tokens"
