"""Tests for DaimonJWTVerifier.

Failure modes (bad sig, malformed/missing sub, unknown account) collapse
to None → HTTP 401 via RequireAuthMiddleware.

jti-revocation tests (Phase 77 PHASE-77-TOKEN-01):
- Revoked jti → verify_token returns None (401).
- Un-revoked agent token → still verifies.
- No jti claim → verifies unchanged (existing non-agent flow unaffected).
- Malformed jti string → verify_token returns None (fail-closed).
"""

from __future__ import annotations

import datetime as dt
import uuid

import jwt as pyjwt
import pytest
from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
from daimon.core.mcp_auth import mint_agent_mcp_token
from daimon.core.stores.mcp_tokens import revoke_mcp_token
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .factories import seed_tenant_and_account

pytestmark = pytest.mark.asyncio

SECRET = b"a" * 32


async def test_verifier_accepts_known_account(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as s, s.begin():
        _tenant_id, account_id = await seed_tenant_and_account(s)
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    token = pyjwt.encode({"sub": str(account_id), "iat": 0}, SECRET, algorithm="HS256")

    result = await verifier.verify_token(token)

    assert result is not None, "known account must accept"
    assert result.claims["sub"] == str(account_id), "claims should carry sub"


async def test_verifier_stashes_tenant_id_in_claims(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as s, s.begin():
        tenant_id, account_id = await seed_tenant_and_account(s)
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    token = pyjwt.encode({"sub": str(account_id), "iat": 0}, SECRET, algorithm="HS256")

    result = await verifier.verify_token(token)

    assert result is not None
    assert result.claims["tenant_id"] == str(tenant_id), (
        "verifier must stash tenant_id from account row into claims"
    )


async def test_verifier_rejects_bad_signature(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    token = pyjwt.encode({"sub": str(uuid.uuid4()), "iat": 0}, b"b" * 32, algorithm="HS256")
    assert await verifier.verify_token(token) is None


async def test_verifier_rejects_missing_sub(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    token = pyjwt.encode({"iat": 0}, SECRET, algorithm="HS256")
    assert await verifier.verify_token(token) is None


async def test_verifier_rejects_malformed_uuid_sub(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    token = pyjwt.encode({"sub": "not-a-uuid", "iat": 0}, SECRET, algorithm="HS256")
    assert await verifier.verify_token(token) is None


async def test_verifier_rejects_unknown_account(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    token = pyjwt.encode({"sub": str(uuid.uuid4()), "iat": 0}, SECRET, algorithm="HS256")
    assert await verifier.verify_token(token) is None


# ---------------------------------------------------------------------------
# jti-revocation tests (Phase 77 PHASE-77-TOKEN-01)
# ---------------------------------------------------------------------------


async def test_verifier_rejects_revoked_jti_with_none(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A token whose jti is revoked makes verify_token return None (→ HTTP 401).

    mint_agent_mcp_token writes the jti row; revoke_mcp_token marks it
    revoked; the verifier must then reject the still-signed token.
    """
    # Use a future now so the exp claim (now + 90d) is not yet expired.
    now = dt.datetime(2099, 1, 1, tzinfo=dt.UTC)
    async with sessionmaker() as s, s.begin():
        tenant_id, account_id = await seed_tenant_and_account(s)
        agent_id = uuid.uuid4()
        token = await mint_agent_mcp_token(
            s,
            account_id=account_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            label="test",
            secret=SECRET,
            now=now,
            ttl_days=90,
        )
        # Decode to get jti without verifying sig (just claim extraction)
        claims = pyjwt.decode(token, SECRET, algorithms=["HS256"])
        jti = uuid.UUID(claims["jti"])
        await revoke_mcp_token(s, jti=jti, now=now)

    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    result = await verifier.verify_token(token)
    assert result is None, (
        "verify_token must return None (→ HTTP 401) for a token whose jti is revoked"
    )


async def test_verifier_accepts_unrevoked_agent_token(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A freshly minted (un-revoked) agent token with a jti claim still verifies."""
    # Use a future now so the exp claim (now + 90d) is not yet expired.
    now = dt.datetime(2099, 1, 1, tzinfo=dt.UTC)
    async with sessionmaker() as s, s.begin():
        tenant_id, account_id = await seed_tenant_and_account(s)
        agent_id = uuid.uuid4()
        token = await mint_agent_mcp_token(
            s,
            account_id=account_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            label="test",
            secret=SECRET,
            now=now,
            ttl_days=90,
        )

    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    result = await verifier.verify_token(token)
    assert result is not None, (
        "verify_token must return a valid AccessToken for an un-revoked agent token"
    )
    assert result.claims["sub"] == str(account_id), (
        "un-revoked agent token must carry the correct sub claim"
    )


async def test_verifier_accepts_no_jti_token_unchanged(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A token with no jti claim (e.g. mint_jwt output) verifies unchanged.

    The jti check branch only fires when a jti claim is present — existing
    mint_jwt and mint_internal_mcp_token flows must be unaffected.
    """
    async with sessionmaker() as s, s.begin():
        _tenant_id, account_id = await seed_tenant_and_account(s)
    # mint_jwt produces {sub, iat} — no jti, no exp
    token = pyjwt.encode({"sub": str(account_id), "iat": 0}, SECRET, algorithm="HS256")

    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    result = await verifier.verify_token(token)
    assert result is not None, (
        "verify_token must accept a token with no jti (existing mint_jwt flow unaffected)"
    )


async def test_verifier_rejects_malformed_jti_string(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A jti claim that is present but not a valid UUID makes verify_token return None.

    Fail-closed: a bad jti string is rejected rather than silently skipped,
    so a crafted token with a non-UUID jti claim cannot bypass the revocation check.
    """
    async with sessionmaker() as s, s.begin():
        _tenant_id, account_id = await seed_tenant_and_account(s)
    token = pyjwt.encode(
        {"sub": str(account_id), "jti": "not-a-uuid", "iat": 0},
        SECRET,
        algorithm="HS256",
    )

    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    result = await verifier.verify_token(token)
    assert result is None, (
        "verify_token must return None (fail-closed) when jti is present but not a valid UUID"
    )
