"""Tests for daimon.core.mcp_auth mint functions.

The verifier side is covered in the MCP adapter test suite (DaimonJWTVerifier
integration); here we assert the pure shape: sub=account_uuid, iat injected,
no other claims, HS256 signature round-trips with PyJWT.

Phase 77 (PHASE-77-TOKEN-01): extends with mint_agent_mcp_token round-trip,
A1 exact-claim-set assertion, and store-row-readable verification.
"""

from __future__ import annotations

import datetime as dt
import uuid

import jwt as pyjwt
from daimon.core.mcp_auth import mint_agent_mcp_token, mint_internal_mcp_token, mint_jwt
from sqlalchemy.ext.asyncio import AsyncSession


def test_mint_jwt_shape() -> None:
    account_id = uuid.uuid4()
    secret = b"a" * 32
    now = dt.datetime(2026, 4, 24, 12, 0, 0, tzinfo=dt.UTC)

    token = mint_jwt(account_id=account_id, secret=secret, now=now)

    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert decoded == {
        "sub": str(account_id),
        "iat": int(now.timestamp()),
    }, "mint_jwt should emit exactly sub + iat, no other claims"


def test_mint_jwt_omits_agent_id_claim_when_not_passed() -> None:
    """Phase 19: backward compat — no agent_id kwarg → no agent_id claim."""
    account_id = uuid.uuid4()
    secret = b"a" * 32
    now = dt.datetime(2026, 4, 24, 12, 0, 0, tzinfo=dt.UTC)

    token = mint_jwt(account_id=account_id, secret=secret, now=now)

    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert "agent_id" not in decoded, "absent agent_id kwarg must not add the claim"


def test_mint_jwt_includes_agent_id_claim_when_passed() -> None:
    """Phase 19 (D-10/D-27): mint_jwt encodes the optional agent_id claim."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    secret = b"a" * 32
    now = dt.datetime(2026, 4, 24, 12, 0, 0, tzinfo=dt.UTC)

    token = mint_jwt(account_id=account_id, secret=secret, now=now, agent_id=agent_id)

    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert decoded.get("agent_id") == str(agent_id), (
        "agent_id kwarg should land in the JWT body as a string"
    )
    assert decoded == {
        "sub": str(account_id),
        "iat": int(now.timestamp()),
        "agent_id": str(agent_id),
    }, "no other claims should appear when agent_id is supplied"


def test_mint_jwt_never_emits_platform_or_guild_id_claims() -> None:
    """Minted tokens must carry no platform or guild_id wire claims."""
    account_id = uuid.uuid4()
    secret = b"a" * 32
    now = dt.datetime(2026, 4, 24, 12, 0, 0, tzinfo=dt.UTC)

    token = mint_jwt(account_id=account_id, secret=secret, now=now)

    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert "platform" not in decoded, "minted token must carry no platform wire claim"
    assert "guild_id" not in decoded, "minted token must carry no guild_id wire claim"


def test_mint_jwt_agent_id_does_not_introduce_platform_or_guild_id_claims() -> None:
    """agent_id claim is orthogonal; no platform/guild_id appears alongside it."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    secret = b"a" * 32
    now = dt.datetime(2026, 4, 24, 12, 0, 0, tzinfo=dt.UTC)

    token = mint_jwt(
        account_id=account_id,
        secret=secret,
        now=now,
        agent_id=agent_id,
    )

    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert decoded["agent_id"] == str(agent_id), "agent_id claim must be present"
    assert "platform" not in decoded, "minted token must carry no platform wire claim"
    assert "guild_id" not in decoded, "minted token must carry no guild_id wire claim"


# ---- Phase 50: is_admin claim (Wave 0 RED stubs) ----


def test_mint_jwt_emits_is_admin_claim_when_true() -> None:
    """mint_jwt should emit is_admin claim when admin."""
    account_id = uuid.uuid4()
    secret = b"a" * 32
    now = dt.datetime(2026, 5, 29, 12, 0, 0, tzinfo=dt.UTC)

    token = mint_jwt(account_id=account_id, secret=secret, now=now, is_admin=True)

    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert decoded["is_admin"] is True, "mint_jwt should emit is_admin claim when admin"


def test_mint_jwt_omits_is_admin_claim_when_false() -> None:
    """mint_jwt should omit is_admin claim for non-admin to keep token minimal."""
    account_id = uuid.uuid4()
    secret = b"a" * 32
    now = dt.datetime(2026, 5, 29, 12, 0, 0, tzinfo=dt.UTC)

    token_explicit = mint_jwt(account_id=account_id, secret=secret, now=now, is_admin=False)
    token_default = mint_jwt(account_id=account_id, secret=secret, now=now)

    decoded_explicit = pyjwt.decode(token_explicit, secret, algorithms=["HS256"])
    decoded_default = pyjwt.decode(token_default, secret, algorithms=["HS256"])
    assert "is_admin" not in decoded_explicit, (
        "mint_jwt should omit is_admin claim for non-admin to keep token minimal"
    )
    assert "is_admin" not in decoded_default, (
        "mint_jwt should omit is_admin claim when kwarg absent (default non-admin)"
    )


def test_mint_internal_mcp_token_emits_is_admin_true() -> None:
    """headless/routine token is admin per D-50b."""
    account_id = uuid.uuid4()
    secret = b"a" * 32
    now = dt.datetime(2026, 5, 29, 12, 0, 0, tzinfo=dt.UTC)

    token = mint_internal_mcp_token(
        account_id=account_id,
        secret=secret,
        now=now,
    )

    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert decoded["is_admin"] is True, "headless/routine token is admin per D-50b"


# ---- Phase 88-03: internal discriminator on mint_internal_mcp_token; admin_ttl_seconds REMOVED ----


def test_mint_jwt_default_omits_exp() -> None:
    """Default mint_jwt produces no exp claim (long-lived, no expiry)."""
    account_id = uuid.uuid4()
    secret = b"a" * 32
    now = dt.datetime(2026, 5, 29, 12, 0, 0, tzinfo=dt.UTC)

    token = mint_jwt(account_id=account_id, secret=secret, now=now)

    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert "exp" not in decoded, (
        "default mint_jwt must omit exp — long-lived vault credentials have no expiry"
    )
    assert "is_admin" not in decoded, (
        "default mint_jwt must omit is_admin — token is non-admin by default"
    )


def test_mint_internal_mcp_token_has_no_exp() -> None:
    """Regression: mint_internal_mcp_token must NOT gain an exp claim.

    Phase 88 touches mint_jwt but must not touch mint_internal_mcp_token
    (D-02/D-50b). Headless/routine tokens are trusted-context, per-invocation,
    and long-lived by design — no exp.
    """
    account_id = uuid.uuid4()
    secret = b"a" * 32
    now = dt.datetime(2026, 5, 29, 12, 0, 0, tzinfo=dt.UTC)

    token = mint_internal_mcp_token(account_id=account_id, secret=secret, now=now)

    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert decoded["is_admin"] is True, (
        "mint_internal_mcp_token must still emit is_admin=True (D-50b regression)"
    )
    assert "exp" not in decoded, (
        "mint_internal_mcp_token must NOT carry exp — it is unchanged by Phase 88 (D-02)"
    )


def test_mint_internal_mcp_token_emits_internal_claim() -> None:
    """ADMIN-02: mint_internal_mcp_token emits internal=True as the trusted-token discriminator.

    This is the positive discriminator that separates an internal (CLI/scheduler/headless)
    token from a Discord vault token minted by mint_jwt. The MCP admin gate keys on this
    claim (along with is_admin) to grant admin elevation to internal tokens without trusting
    baked is_admin from Discord vault creds.
    """
    account_id = uuid.uuid4()
    secret = b"a" * 32
    now = dt.datetime(2026, 5, 29, 12, 0, 0, tzinfo=dt.UTC)

    token = mint_internal_mcp_token(account_id=account_id, secret=secret, now=now)

    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert decoded.get("internal") is True, (
        "mint_internal_mcp_token must emit internal=True as the trusted-token discriminator "
        "(ADMIN-02); the MCP gate keys on is_admin AND internal together — never on is_admin alone"
    )


def test_mint_jwt_never_emits_internal_claim() -> None:
    """ADMIN-01: mint_jwt (Discord/CLI path) must NEVER emit the internal claim.

    A Discord vault token carrying is_admin=True but no internal claim cannot
    elevate a non-admin caller even if the is_admin baking was stale/incorrect.
    This test proves the gate is closed without relying on the 88-06 sweep.
    """
    account_id = uuid.uuid4()
    secret = b"a" * 32
    now = dt.datetime(2026, 5, 29, 12, 0, 0, tzinfo=dt.UTC)

    token_default = mint_jwt(account_id=account_id, secret=secret, now=now)
    token_is_admin = mint_jwt(account_id=account_id, secret=secret, now=now, is_admin=True)

    for label, token in [("default", token_default), ("is_admin=True", token_is_admin)]:
        decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
        assert "internal" not in decoded, (
            f"mint_jwt ({label}) must NEVER emit the internal claim — "
            "only mint_internal_mcp_token is trusted; a Discord vault token with is_admin=True "
            "and no internal claim is denied admin elevation at the gate (ADMIN-01/closes #162)"
        )


# ---- Phase 77: mint_agent_mcp_token (A1 claim shape + store round-trip) ----


async def _seed_tenant_and_account(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    from daimon.core._models import Account, Tenant
    from daimon.core.ma_identity import derive_tenant_uuid

    guild_id = str(uuid.uuid4())
    tenant = Tenant(
        id=derive_tenant_uuid(platform="discord", workspace_id=guild_id),
        platform="discord",
        external_id=guild_id,
    )
    session.add(tenant)
    await session.flush()
    account = Account(tenant_id=tenant.id)
    session.add(account)
    await session.flush()
    await session.refresh(account)
    return tenant.id, account.id


async def test_mint_agent_mcp_token_claim_set_is_exactly_sub_agent_id_jti_exp(
    db_session: AsyncSession,
) -> None:
    """A1: minted token must carry exactly {sub, agent_id, jti, exp} — no role, no tenant_id."""
    tenant_id, account_id = await _seed_tenant_and_account(db_session)
    agent_id = uuid.uuid4()
    secret = b"a" * 32
    now = dt.datetime(2026, 6, 23, 12, 0, 0, tzinfo=dt.UTC)

    token = await mint_agent_mcp_token(
        db_session,
        account_id=account_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        label="test-agent",
        secret=secret,
        now=now,
    )

    # exp is in the future — decode with leeway=0
    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert set(decoded.keys()) == {"sub", "agent_id", "jti", "exp"}, (
        "A1: minted agent token must carry exactly {sub, agent_id, jti, exp} — "
        "role and tenant_id must NOT be minted (verifier re-derives both from DB)"
    )
    assert "role" not in decoded, "role must not appear in the minted agent token (A1)"
    assert "tenant_id" not in decoded, "tenant_id must not appear in the minted agent token (A1)"


async def test_mint_agent_mcp_token_sub_and_agent_id_claims_match_inputs(
    db_session: AsyncSession,
) -> None:
    """sub = str(account_id), agent_id = str(agent_id UUID), jti is a valid UUID string."""
    tenant_id, account_id = await _seed_tenant_and_account(db_session)
    agent_id = uuid.uuid4()
    secret = b"b" * 32
    now = dt.datetime(2026, 6, 23, 12, 0, 0, tzinfo=dt.UTC)

    token = await mint_agent_mcp_token(
        db_session,
        account_id=account_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        label=None,
        secret=secret,
        now=now,
    )

    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert decoded["sub"] == str(account_id), "sub must be the stringified account_id"
    assert decoded["agent_id"] == str(agent_id), "agent_id claim must be str(agent_id UUID)"
    # jti must be a valid UUID
    jti_claim = decoded["jti"]
    parsed_jti = uuid.UUID(jti_claim)
    assert str(parsed_jti) == jti_claim, "jti claim must be a valid UUID string"


async def test_mint_agent_mcp_token_exp_is_ttl_days_from_now(
    db_session: AsyncSession,
) -> None:
    """exp = int((now + ttl_days).timestamp()); default ttl_days=90."""
    tenant_id, account_id = await _seed_tenant_and_account(db_session)
    secret = b"c" * 32
    now = dt.datetime(2026, 6, 23, 12, 0, 0, tzinfo=dt.UTC)
    expected_exp = int((now + dt.timedelta(days=90)).timestamp())

    token = await mint_agent_mcp_token(
        db_session,
        account_id=account_id,
        tenant_id=tenant_id,
        agent_id=uuid.uuid4(),
        label=None,
        secret=secret,
        now=now,
    )

    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert decoded["exp"] == expected_exp, (
        "exp must equal int((now + 90d).timestamp()) — default ttl_days=90"
    )


async def test_mint_agent_mcp_token_writes_row_readable_by_get_mcp_token(
    db_session: AsyncSession,
) -> None:
    """mint_agent_mcp_token writes a row; get_mcp_token returns it by jti."""
    from daimon.core.stores.mcp_tokens import get_mcp_token

    tenant_id, account_id = await _seed_tenant_and_account(db_session)
    agent_id = uuid.uuid4()
    secret = b"d" * 32
    now = dt.datetime(2026, 6, 23, 12, 0, 0, tzinfo=dt.UTC)

    token = await mint_agent_mcp_token(
        db_session,
        account_id=account_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        label="my-agent",
        secret=secret,
        now=now,
    )

    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    jti = uuid.UUID(decoded["jti"])

    row = await get_mcp_token(db_session, jti=jti)
    assert row is not None, "mint_agent_mcp_token must write a row readable by get_mcp_token"
    assert row.agent_id == str(agent_id), (
        "row.agent_id must equal str(agent_id) (A2 — stringified derived UUID)"
    )
    assert row.revoked_at is None, "freshly minted token row must not be revoked"
