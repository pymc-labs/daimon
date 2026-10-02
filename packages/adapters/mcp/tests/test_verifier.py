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
    REFUSAL_AUDIT_TOOL,
    SCOPES_CLAIM,
    TOKEN_JTI_CLAIM,
    TOKEN_KIND_CLAIM,
    DaimonJWTVerifier,
)
from daimon.core.mcp_auth import mint_agent_mcp_token, mint_cli_mcp_token, mint_operator_mcp_token
from daimon.core.operator_tokens import OperatorScope
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.mcp_tokens import get_mcp_token, revoke_mcp_token
from daimon.core.stores.security_audit import list_events
from sqlalchemy import text
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


_SET_SCOPES = text("UPDATE mcp_tokens SET scopes = :scopes WHERE jti = :jti")


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
        await s.execute(_SET_SCOPES, {"scopes": ["tenant:read"], "jti": _jti(token)})
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
            text("UPDATE mcp_tokens SET expires_at = :at WHERE jti = :jti"),
            {"at": dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1), "jti": _jti(token)},
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
        await s.execute(_SET_SCOPES, {"scopes": [], "jti": _jti(token)})
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


@pytest.mark.parametrize("reason", ["revoked", "expired", "not_admin", "kind_mismatch"])
async def test_verifier_audits_each_refusal_of_a_registered_token(
    sessionmaker: async_sessionmaker[AsyncSession], reason: str
) -> None:
    """A refused row leaves an audit row with its tenant, kind, jti and reason, never the token."""
    account_id, token = await _mint_operator(sessionmaker)
    jti = _jti(token)
    async with sessionmaker() as s, s.begin():
        if reason == "revoked":
            await revoke_mcp_token(s, jti=jti, now=dt.datetime.now(dt.UTC))
        elif reason == "expired":
            await s.execute(
                text("UPDATE mcp_tokens SET expires_at = :at WHERE jti = :jti"),
                {"at": dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1), "jti": jti},
            )
        elif reason == "not_admin":
            await set_role(s, account_id, Role.USER)
    if reason == "kind_mismatch":
        claims = pyjwt.decode(token, options={"verify_signature": False})
        token = pyjwt.encode({**claims, "kind": "cli"}, SECRET, algorithm="HS256")
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)

    assert await verifier.verify_token(token) is None, f"a {reason} token gets a 401"

    async with sessionmaker() as s:
        row = await get_mcp_token(s, jti=jti)
        assert row is not None, "the refused token's row stays"
        events = await list_events(s, tenant_id=row.tenant_id)
    assert [
        (e.tool_name, e.outcome, e.reason, e.token_kind, e.token_jti, e.account_id) for e in events
    ] == [(REFUSAL_AUDIT_TOOL, "denied", reason, "operator", jti, account_id)], (
        "the refusal is audited under the token's tenant"
    )
    assert token not in events[0].model_dump_json(), "the token value is never stored"


async def test_verifier_computes_the_administered_channels_from_stored_roles(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The grant is read from the DB; a claim the token brings is overwritten."""
    from daimon.core.stores.accounts import set_platform_role_ids
    from daimon.core.stores.channel_admins import set_channel_admins
    from daimon.testing.factories import make_account, make_platform_principal, make_tenant

    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="discord")
        account = await make_account(s, tenant=tenant)
        await make_platform_principal(
            s, platform="discord", external_id="u1", tenant=tenant, account=account
        )
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    token = pyjwt.encode(
        {"sub": str(account.id), "iat": 0, "administered_channel_ids": ["c9"]},
        SECRET,
        algorithm="HS256",
    )

    before = await verifier.verify_token(token)
    assert before is not None and before.claims["administered_channel_ids"] == [], (
        "no grant yet, whatever the token claims"
    )
    async with sessionmaker() as s, s.begin():
        await set_platform_role_ids(s, account.id, ["r1"])
        await set_channel_admins(
            s,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="c1",
            role_ids=["r1"],
            user_ids=[],
            actor_account_id=None,
        )
    after = await verifier.verify_token(token)
    assert after is not None and after.claims["administered_channel_ids"] == ["c1"], (
        "a new grant shows on the next verify"
    )
    assert after.claims["platform_role_ids"] == ["r1"], "the stored role ids ride along"


async def _agent_token(
    sessionmaker: async_sessionmaker[AsyncSession], *, platform: str | None, channel_id: str | None
) -> str:
    async with sessionmaker() as s, s.begin():
        tenant_id, account_id = await seed_tenant_and_account(s)
        return await mint_agent_mcp_token(
            s,
            account_id=account_id,
            tenant_id=tenant_id,
            agent_id=uuid.uuid4(),
            label="test",
            secret=SECRET,
            now=dt.datetime(2099, 1, 1, tzinfo=dt.UTC),
            platform=platform,
            channel_id=channel_id,
        )


async def test_verifier_reads_the_bound_channel_from_the_token_row(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    token = await _agent_token(sessionmaker, platform="discord", channel_id="c-1")
    result = await DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker).verify_token(token)
    assert result is not None
    assert result.claims["bound_channel_id"] == "c-1"


async def test_verifier_drops_a_bound_channel_claimed_in_the_jwt(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Only the registry row binds a token; a signed claim of its own does not."""
    unbound = await _agent_token(sessionmaker, platform=None, channel_id=None)
    claims = pyjwt.decode(unbound, SECRET, algorithms=["HS256"])
    forged = pyjwt.encode({**claims, "bound_channel_id": "c-1"}, SECRET, algorithm="HS256")
    async with sessionmaker() as s, s.begin():
        _tenant_id, account_id = await seed_tenant_and_account(s)
    no_jti = pyjwt.encode(
        {"sub": str(account_id), "iat": 0, "bound_channel_id": "c-1"}, SECRET, algorithm="HS256"
    )
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    for token in (forged, no_jti):
        result = await verifier.verify_token(token)
        assert result is not None
        assert "bound_channel_id" not in result.claims


async def test_verifier_rejects_a_binding_on_another_platform(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    token = await _agent_token(sessionmaker, platform="slack", channel_id="C1")
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    assert await verifier.verify_token(token) is None, (
        "a Discord account's key binds no Slack channel"
    )
