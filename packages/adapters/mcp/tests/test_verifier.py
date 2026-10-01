"""Tests for DaimonJWTVerifier.

Failure modes (bad sig, malformed/missing sub, unknown account) collapse
to None → HTTP 401 via RequireAuthMiddleware.

jti-revocation tests:
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
from daimon.adapters.mcp.auth.verifier import (
    SCOPES_CLAIM,
    TOKEN_JTI_CLAIM,
    TOKEN_KIND_CLAIM,
    DaimonJWTVerifier,
)
from daimon.core._models import McpToken
from daimon.core.mcp_auth import mint_agent_mcp_token, mint_cli_mcp_token, mint_operator_mcp_token
from daimon.core.operator_tokens import OperatorScope
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.mcp_tokens import revoke_mcp_token
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import seed_server_admin, seed_tenant_and_account

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
# jti-revocation tests
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


async def test_verifier_rejects_a_hub_login_token_signed_with_the_same_secret(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A hub mount's token carries no account subject, only the login's tenant map, so
    even one signed with this verifier's secret must not open the per-agent surface."""
    token = pyjwt.encode(
        {
            "iss": "https://example.test/slack",
            "aud": "https://example.test/slack/mcp",
            "iat": 0,
            "upstream_claims": {"platform": "slack", "platform_user_id": "U1", "tenants": []},
        },
        SECRET,
        algorithm="HS256",
    )

    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    result = await verifier.verify_token(token)
    assert result is None, "a token without an account sub must be rejected by the /mcp verifier"


async def _mint_operator(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    scopes: frozenset[OperatorScope] = frozenset({"tenant:read"}),
    platform_user_id: str | None = "u-admin",
) -> tuple[uuid.UUID, str]:
    async with sessionmaker() as s, s.begin():
        tenant_id, account_id = await seed_server_admin(s, platform_user_id=platform_user_id)
        token = await mint_operator_mcp_token(
            s,
            account_id=account_id,
            tenant_id=tenant_id,
            scopes=scopes,
            label=None,
            secret=SECRET,
            now=dt.datetime.now(dt.UTC),
            ttl_days=30,
        )
    return account_id, token


def _jti(token: str) -> uuid.UUID:
    return uuid.UUID(pyjwt.decode(token, options={"verify_signature": False})["jti"])


async def test_verifier_accepts_operator_token_and_stashes_its_row(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _account_id, token = await _mint_operator(sessionmaker)
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)

    result = await verifier.verify_token(token)

    assert result is not None, "a live operator token for a server admin verifies"
    assert result.claims[TOKEN_KIND_CLAIM] == "operator", "the kind comes from the row"
    assert result.claims[TOKEN_JTI_CLAIM] == str(_jti(token)), "the jti comes from the row"
    assert result.claims[SCOPES_CLAIM] == ["tenant:read"], "scopes come from the row"
    assert result.claims["platform_user_id"] == "u-admin", "the account's platform user is set"


async def test_verifier_reads_operator_scopes_live_from_the_row(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _account_id, token = await _mint_operator(
        sessionmaker, scopes=frozenset({"tenant:read", "channels:write"})
    )
    async with sessionmaker() as s, s.begin():
        await s.execute(
            update(McpToken).where(McpToken.jti == _jti(token)).values(scopes=["tenant:read"])
        )
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)

    result = await verifier.verify_token(token)

    assert result is not None and result.claims[SCOPES_CLAIM] == ["tenant:read"], (
        "narrowing the row's scopes applies to the next request"
    )


async def test_verifier_refuses_operator_token_once_its_admin_is_demoted(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    account_id, token = await _mint_operator(sessionmaker)
    async with sessionmaker() as s, s.begin():
        await set_role(s, account_id, Role.USER)
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)

    assert await verifier.verify_token(token) is None, "a demoted admin's token gets a 401"


async def test_verifier_refuses_revoked_operator_token(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _account_id, token = await _mint_operator(sessionmaker)
    async with sessionmaker() as s, s.begin():
        await revoke_mcp_token(s, jti=_jti(token), now=dt.datetime.now(dt.UTC))
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)

    assert await verifier.verify_token(token) is None, "a revoked token gets a 401"


async def test_verifier_refuses_operator_token_whose_row_expired(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _account_id, token = await _mint_operator(sessionmaker)
    async with sessionmaker() as s, s.begin():
        await s.execute(
            update(McpToken)
            .where(McpToken.jti == _jti(token))
            .values(expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
        )
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)

    assert await verifier.verify_token(token) is None, "the row's expiry is enforced"


async def test_verifier_refuses_operator_token_past_its_exp_claim(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as s, s.begin():
        tenant_id, account_id = await seed_server_admin(s)
        token = await mint_operator_mcp_token(
            s,
            account_id=account_id,
            tenant_id=tenant_id,
            scopes=frozenset({"tenant:read"}),
            label=None,
            secret=SECRET,
            now=dt.datetime.now(dt.UTC) - dt.timedelta(days=31),
            ttl_days=30,
        )
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)

    assert await verifier.verify_token(token) is None, "an expired token gets a 401"


async def test_verifier_refuses_operator_token_without_a_platform_user(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _account_id, token = await _mint_operator(sessionmaker, platform_user_id=None)
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)

    assert await verifier.verify_token(token) is None, (
        "without a platform user the token would read as the unbilled operator path"
    )


async def test_verifier_refuses_operator_token_with_no_scopes_left(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _account_id, token = await _mint_operator(sessionmaker)
    async with sessionmaker() as s, s.begin():
        await s.execute(update(McpToken).where(McpToken.jti == _jti(token)).values(scopes=[]))
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)

    assert await verifier.verify_token(token) is None, "an emptied token grants nothing"


@pytest.mark.parametrize("kind", ["operator", "cli"])
async def test_verifier_refuses_kind_claim_without_a_registered_jti(
    sessionmaker: async_sessionmaker[AsyncSession], kind: str
) -> None:
    async with sessionmaker() as s, s.begin():
        _tenant_id, account_id = await seed_server_admin(s)
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    token = pyjwt.encode(
        {"sub": str(account_id), "iat": 0, "kind": kind, SCOPES_CLAIM: ["promo:create"]},
        SECRET,
        algorithm="HS256",
    )

    assert await verifier.verify_token(token) is None, "only registered tokens carry a kind"


async def test_verifier_accepts_registered_cli_token(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as s, s.begin():
        tenant_id, account_id = await seed_tenant_and_account(s)
        token = await mint_cli_mcp_token(
            s,
            account_id=account_id,
            tenant_id=tenant_id,
            secret=SECRET,
            now=dt.datetime.now(dt.UTC),
            ttl_days=90,
        )
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)

    result = await verifier.verify_token(token)

    assert result is not None and result.claims[TOKEN_KIND_CLAIM] == "cli", (
        "a registered CLI token verifies"
    )
    assert SCOPES_CLAIM not in result.claims, "only operator tokens carry scopes"
