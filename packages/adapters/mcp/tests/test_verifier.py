"""Tests for DaimonJWTVerifier.

Failure modes (bad sig, malformed/missing sub, unknown account) collapse
to None → HTTP 401 via RequireAuthMiddleware.

jti-revocation tests:
- Revoked jti → verify_token returns None (401).
- Un-revoked agent token → still verifies.
- No jti claim → obeys the explicitly configured legacy cutoff.
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
    would_coding_key_pass,
)
from daimon.core.mcp_auth import (
    mint_agent_mcp_token,
    mint_chat_mcp_token,
    mint_cli_mcp_token,
    mint_operator_mcp_token,
)
from daimon.core.operator_tokens import OperatorScope
from daimon.core.stores.accounts import set_role
from daimon.core.stores.agent_owners import clear_owner, set_owner
from daimon.core.stores.domain import Role
from daimon.core.stores.mcp_tokens import get_mcp_token, revoke_mcp_token
from daimon.core.stores.security_audit import list_events
from daimon.core.turn_hold import TurnHold
from daimon.testing.factories import make_account, make_cli_principal, make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import seed_server_admin, seed_tenant_and_account

SECRET = b"a" * 32
LEGACY_CUTOFF = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)


async def test_verifier_accepts_known_account(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as s, s.begin():
        _tenant_id, account_id = await seed_tenant_and_account(s)
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )
    token = pyjwt.encode({"sub": str(account_id), "iat": 0}, SECRET, algorithm="HS256")

    result = await verifier.verify_token(token)

    assert result is not None, "known account must accept"
    assert result.claims["sub"] == str(account_id), "claims should carry sub"


async def test_verifier_stashes_tenant_id_in_claims(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as s, s.begin():
        tenant_id, account_id = await seed_tenant_and_account(s)
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )
    token = pyjwt.encode({"sub": str(account_id), "iat": 0}, SECRET, algorithm="HS256")

    result = await verifier.verify_token(token)

    assert result is not None
    assert result.claims["tenant_id"] == str(tenant_id), (
        "verifier must stash tenant_id from account row into claims"
    )


async def test_verifier_rejects_bad_signature(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )
    token = pyjwt.encode({"sub": str(uuid.uuid4()), "iat": 0}, b"b" * 32, algorithm="HS256")
    assert await verifier.verify_token(token) is None


async def test_verifier_rejects_missing_sub(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )
    token = pyjwt.encode({"iat": 0}, SECRET, algorithm="HS256")
    assert await verifier.verify_token(token) is None


async def test_verifier_rejects_malformed_uuid_sub(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )
    token = pyjwt.encode({"sub": "not-a-uuid", "iat": 0}, SECRET, algorithm="HS256")
    assert await verifier.verify_token(token) is None


async def test_verifier_rejects_unknown_account(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )
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

    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )
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
        tenant_id, account_id = await seed_server_admin(s)
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

    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )
    result = await verifier.verify_token(token)
    assert result is not None, (
        "verify_token must return a valid AccessToken for an un-revoked agent token"
    )
    assert result.claims["sub"] == str(account_id), (
        "un-revoked agent token must carry the correct sub claim"
    )


async def test_verifier_accepts_no_jti_token_at_explicit_legacy_cutoff(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A token at the explicitly configured cutoff remains compatible.

    The jti check branch only fires when a jti claim is present — existing
    The default cutoff and old-bearer rejection have dedicated tests below.
    """
    async with sessionmaker() as s, s.begin():
        _tenant_id, account_id = await seed_tenant_and_account(s)
    # mint_jwt produces {sub, iat} — no jti, no exp
    token = pyjwt.encode({"sub": str(account_id), "iat": 0}, SECRET, algorithm="HS256")

    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )
    result = await verifier.verify_token(token)
    assert result is not None, "a legacy token at the configured cutoff remains admitted"


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

    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )
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

    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )
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
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )

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
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )

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
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )

    assert await verifier.verify_token(token) is None, "a demoted admin's token gets a 401"


async def test_verifier_refuses_revoked_operator_token(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _account_id, token = await _mint_operator(sessionmaker)
    async with sessionmaker() as s, s.begin():
        await revoke_mcp_token(s, jti=_jti(token), now=dt.datetime.now(dt.UTC))
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )

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
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )

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
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )

    assert await verifier.verify_token(token) is None, "an expired token gets a 401"


async def test_verifier_refuses_operator_token_without_a_platform_user(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _account_id, token = await _mint_operator(sessionmaker, platform_user_id=None)
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )

    assert await verifier.verify_token(token) is None, (
        "without a platform user the token would read as the unbilled operator path"
    )


async def test_verifier_refuses_operator_token_with_no_scopes_left(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _account_id, token = await _mint_operator(sessionmaker)
    async with sessionmaker() as s, s.begin():
        await s.execute(_SET_SCOPES, {"scopes": [], "jti": _jti(token)})
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )

    assert await verifier.verify_token(token) is None, "an emptied token grants nothing"


@pytest.mark.parametrize("kind", ["operator", "cli"])
async def test_verifier_refuses_kind_claim_without_a_registered_jti(
    sessionmaker: async_sessionmaker[AsyncSession], kind: str
) -> None:
    async with sessionmaker() as s, s.begin():
        _tenant_id, account_id = await seed_server_admin(s)
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )
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
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )

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
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )

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


async def _agent_token(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str | None,
    channel_id: str | None,
    admin: bool = False,
) -> str:
    async with sessionmaker() as s, s.begin():
        tenant_id, account_id = await (seed_server_admin if admin else seed_tenant_and_account)(s)
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


async def test_verifier_refuses_nonadmin_bound_token(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    token = await _agent_token(sessionmaker, platform="discord", channel_id="c-1")
    result = await DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    ).verify_token(token)
    assert result is None


async def test_verifier_drops_a_bound_channel_claimed_in_the_jwt(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Only the registry row binds a token; a signed claim of its own does not."""
    unbound = await _agent_token(sessionmaker, platform=None, channel_id=None, admin=True)
    claims = pyjwt.decode(unbound, SECRET, algorithms=["HS256"])
    forged = pyjwt.encode({**claims, "bound_channel_id": "c-1"}, SECRET, algorithm="HS256")
    async with sessionmaker() as s, s.begin():
        _tenant_id, account_id = await seed_tenant_and_account(s)
    no_jti = pyjwt.encode(
        {"sub": str(account_id), "iat": 0, "bound_channel_id": "c-1"}, SECRET, algorithm="HS256"
    )
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )
    for token in (forged, no_jti):
        result = await verifier.verify_token(token)
        assert result is not None
        assert "bound_channel_id" not in result.claims


async def test_verifier_rejects_a_binding_on_another_platform(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    token = await _agent_token(sessionmaker, platform="slack", channel_id="C1")
    verifier = DaimonJWTVerifier(
        legacy_token_cutoff=LEGACY_CUTOFF,
        allow_legacy_tokens=True,
        secret=SECRET,
        sessionmaker=sessionmaker,
    )
    assert await verifier.verify_token(token) is None, (
        "a Discord account's key binds no Slack channel"
    )
    async with sessionmaker() as s:
        row = await get_mcp_token(s, jti=_jti(token))
        assert row is not None
        events = await list_events(s, tenant_id=row.tenant_id)
    assert [(e.reason, e.token_kind) for e in events] == [("platform_mismatch", "agent")], (
        "the refusal is audited like every other registry refusal"
    )


@pytest.mark.parametrize(
    "corruption",
    [
        None,
        "unknown",
        "revoked",
        "expired",
        "foreign_account",
        "foreign_agent",
        "missing_jti",
        "missing_exp",
        "external_agent_claim",
    ],
)
async def test_vault_chat_jwt_requires_its_live_bound_registry_row(sessionmaker, corruption):
    from daimon.core.mcp_auth import mint_chat_mcp_token

    now = dt.datetime.now(dt.UTC)
    async with sessionmaker() as db, db.begin():
        _tenant, account = await seed_tenant_and_account(db)
        token, jti, _expiry = await mint_chat_mcp_token(
            db, account_id=account, agent_id=uuid.uuid4(), secret=SECRET, now=now
        )
        if corruption == "revoked":
            await revoke_mcp_token(db, jti=jti, now=now)
        if corruption == "foreign_account":
            _, other = await seed_tenant_and_account(db)
    claims = pyjwt.decode(token, SECRET, algorithms=["HS256"])
    if corruption == "unknown":
        claims["jti"] = str(uuid.uuid4())
    if corruption == "expired":
        claims["exp"] = int((now - dt.timedelta(seconds=1)).timestamp())
    if corruption == "foreign_account":
        claims["sub"] = str(other)
    if corruption == "foreign_agent":
        claims["chat_agent_id"] = str(uuid.uuid4())
    if corruption == "missing_jti":
        claims.pop("jti")
    if corruption == "missing_exp":
        claims.pop("exp")
    if corruption == "external_agent_claim":
        claims["agent_id"] = claims.pop("chat_agent_id")
    candidate = pyjwt.encode(claims, SECRET, algorithm="HS256")
    verifier = DaimonJWTVerifier(
        secret=SECRET,
        sessionmaker=sessionmaker,
    )
    result = await verifier.verify_token(candidate)
    assert (result is not None) == (corruption is None)
    if result is not None:
        assert result.claims["chat_agent_id"] == claims["chat_agent_id"]
        assert "agent_id" not in result.claims
        assert result.claims["daimon_token_jti"] == str(jti)


async def test_external_agent_key_label_does_not_select_chat_authentication(sessionmaker):
    """A person's existing human label is not a token-kind discriminator."""
    now = dt.datetime.now(dt.UTC)
    agent_id = uuid.uuid4()
    async with sessionmaker() as db, db.begin():
        tenant_id, account_id = await seed_server_admin(db)
        token = await mint_agent_mcp_token(
            db,
            account_id=account_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            label="vault-chat",
            secret=SECRET,
            now=now,
        )
    result = await DaimonJWTVerifier(
        secret=SECRET,
        sessionmaker=sessionmaker,
    ).verify_token(token)
    assert result is not None and result.claims["agent_id"] == str(agent_id)
    assert "chat_agent_id" not in result.claims


@pytest.mark.parametrize("issued", [0, None, "0", True])
async def test_verifier_default_cutoff_rejects_old_or_missing_legacy_iat(
    sessionmaker: async_sessionmaker[AsyncSession],
    issued: object,
) -> None:
    async with sessionmaker.begin() as session:
        _, account_id = await seed_tenant_and_account(session)
    claims: dict[str, object] = {"sub": str(account_id)}
    if issued is not None:
        claims["iat"] = issued
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    token = pyjwt.encode(claims, SECRET, algorithm="HS256")
    assert await verifier.verify_token(token) is None


async def test_verifier_cutoff_boundary_registered_tokens_and_internal_refusal(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    async with sessionmaker.begin() as session:
        tenant_id, account_id = await seed_tenant_and_account(session)
        registered = await mint_cli_mcp_token(
            session,
            account_id=account_id,
            tenant_id=tenant_id,
            secret=SECRET,
            now=now - dt.timedelta(minutes=1),
            ttl_days=1,
        )
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker, legacy_token_cutoff=now)

    def legacy(issued: int, **extra: object) -> str:
        return pyjwt.encode(
            {"sub": str(account_id), "iat": issued, **extra}, SECRET, algorithm="HS256"
        )

    assert await verifier.verify_token(legacy(int(now.timestamp()) - 1)) is None
    assert await verifier.verify_token(legacy(int(now.timestamp()))) is None
    assert await verifier.verify_token(legacy(int(now.timestamp()) + 600)) is None
    assert await verifier.verify_token(legacy(0, internal=True, is_admin=True)) is None
    assert await verifier.verify_token(legacy(0, internal="true")) is None
    assert await verifier.verify_token(legacy(int(now.timestamp()), jti=12)) is None
    assert await verifier.verify_token(registered) is not None


async def test_temporary_minter_window_closes_for_every_unregistered_token(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.adapters.mcp.auth import verifier as verifier_module

    base = dt.datetime.now(dt.UTC).replace(microsecond=0)
    async with sessionmaker.begin() as session:
        _, account_id = await seed_tenant_and_account(session)
    cutoff = base + dt.timedelta(minutes=30)
    instance = DaimonJWTVerifier(
        secret=SECRET,
        sessionmaker=sessionmaker,
        allow_legacy_tokens=True,
        legacy_token_cutoff=cutoff,
    )
    clock = [base]

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(verifier_module, "datetime", Clock)

    def token(issued: int, **extra: object) -> str:
        return pyjwt.encode(
            {"sub": str(account_id), "iat": issued, **extra}, SECRET, algorithm="HS256"
        )

    old = token(0)
    chat = token(0, chat_agent_id=str(uuid.uuid4()))
    assert await instance.verify_token(old) is not None
    assert await instance.verify_token(chat) is not None
    for internal in [True, False, "true", None]:
        assert await instance.verify_token(token(0, internal=internal, is_admin=True)) is None
    assert await instance.verify_token(token(0, jti=None)) is None
    assert await instance.verify_token(token(0, jti=str(uuid.uuid4()))) is None
    clock[0] = cutoff
    for issued in [0, int(cutoff.timestamp()), int(cutoff.timestamp()) + 60]:
        assert await instance.verify_token(token(issued)) is None
        assert await instance.verify_token(token(issued, chat_agent_id=str(uuid.uuid4()))) is None


@pytest.mark.parametrize("rollout", [False, True])
@pytest.mark.parametrize(
    ("days", "bucket"),
    [(0, "<1d"), (2, "1-7d"), (8, "7-30d"), (31, "30-90d"), (91, ">90d"), (-1, "future")],
)
async def test_internal_legacy_refusals_log_only_age_and_shape(
    sessionmaker: async_sessionmaker[AsyncSession], rollout: bool, days: int, bucket: str
) -> None:
    from daimon.testing.legacy_mcp_tokens import mint_internal_mcp_token
    from structlog.testing import capture_logs

    now = dt.datetime.now(dt.UTC)
    async with sessionmaker.begin() as session:
        _, account_id = await seed_tenant_and_account(session)
    verifier = DaimonJWTVerifier(
        secret=SECRET,
        sessionmaker=sessionmaker,
        allow_legacy_tokens=rollout,
        legacy_token_cutoff=now + dt.timedelta(hours=1),
    )
    token = mint_internal_mcp_token(
        account_id=account_id, secret=SECRET, now=now - dt.timedelta(days=days)
    )
    with capture_logs() as logs:
        assert await verifier.verify_token(token) is None
    events = [event for event in logs if event.get("event") == "mcp.token_rejected"]
    assert len(events) == 1
    event = events[0]
    assert event["reason"] == "unregistered_internal"
    assert event["age_bucket"] == bucket
    assert event["internal_true"] is True
    assert event["has_jti"] is False
    assert event["has_exp"] is False
    assert event["has_sub"] is True
    assert all(
        isinstance(value, bool)
        for key, value in event.items()
        if key not in {"reason", "event", "log_level", "age_bucket"}
    )
    assert not {"sub", "token", "secret", "jti", "iat", "exp"} & event.keys()
    rendered = repr(logs)
    assert token not in rendered
    assert str(account_id) not in rendered
    assert SECRET.decode() not in rendered


async def test_untrusted_rejection_does_not_log_unverified_claim_values(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from structlog.testing import capture_logs

    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    account = str(uuid.uuid4())
    token = pyjwt.encode(
        {"sub": account, "iat": 0, "internal": True, "secret": "never-log-this-value"},
        b"b" * 32,
        algorithm="HS256",
    )
    with capture_logs() as logs:
        assert await verifier.verify_token(token) is None
    events = [event for event in logs if event.get("event") == "mcp.token_rejected"]
    assert len(events) == 1
    assert events[0]["reason"] == "signature_or_expiry"
    assert events[0]["age_bucket"] == "unknown"
    assert events[0]["has_sub"] is False
    assert account not in repr(logs)
    assert token not in repr(logs)
    assert "never-log-this-value" not in repr(logs)


async def test_expired_token_rejection_never_logs_the_dependency_subject(
    sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from structlog.testing import capture_logs

    account = str(uuid.uuid4())
    now = dt.datetime.now(dt.UTC)
    token = pyjwt.encode(
        {"sub": account, "iat": int(now.timestamp()) - 7200, "exp": int(now.timestamp()) - 3600},
        SECRET,
        algorithm="HS256",
    )
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    with caplog.at_level(logging.DEBUG), capture_logs() as logs:
        assert await verifier.verify_token(token) is None
    assert account not in caplog.text + repr(logs)
    assert token not in caplog.text + repr(logs)
    assert logs[-1]["event"] == "mcp.token_rejected"
    assert logs[-1]["reason"] == "signature_or_expiry"
    assert logs[-1]["age_bucket"] == "unknown"


def test_refusal_diagnostics_survive_the_production_redaction_processor() -> None:
    from daimon.adapters.mcp.auth.verifier import _token_diagnostics
    from daimon.core.observability import redact_log_event

    diagnostics = _token_diagnostics({"iat": 0, "sub": "must-not-log", "internal": True})
    event = {"event": "mcp.token_rejected", "reason": "unregistered_internal", **diagnostics}
    assert redact_log_event(None, "warning", event) == event
    assert event["age_bucket"] == ">90d"
    assert "must-not-log" not in repr(event)


@pytest.mark.parametrize(
    "corruption",
    [
        None,
        "revoked",
        "foreign_agent",
        "foreign_account",
        "missing_jti",
        "missing_exp",
        "extra_agent",
        "extra_internal",
        "extra_iat",
        "extra_role",
        "expiry_mismatch",
        "overlong_registry",
        "future_registry",
        "system_label",
    ],
)
async def test_registered_remote_chat_credential_reaches_real_verifier(sessionmaker, corruption):
    now = dt.datetime.now(dt.UTC)
    agent_id = uuid.uuid4()
    async with sessionmaker() as db, db.begin():
        tenant, account = await seed_tenant_and_account(db)
        token = await mint_agent_mcp_token(
            db,
            account_id=account,
            tenant_id=tenant,
            agent_id=agent_id,
            label="source chat",
            secret=SECRET,
            now=now,
            ttl_days=900 / 86400,
            chat_execution=True,
        )
        claims = pyjwt.decode(token, SECRET, algorithms=["HS256"])
        jti = uuid.UUID(claims["jti"])
        if corruption == "revoked":
            await revoke_mcp_token(db, jti=jti, now=now)
        if corruption == "foreign_account":
            _, other = await seed_tenant_and_account(db)
            claims["sub"] = str(other)
        if corruption == "overlong_registry":
            await db.execute(
                text(
                    "UPDATE mcp_tokens SET created_at = created_at - interval '1 second' WHERE jti = :jti"
                ),
                {"jti": jti},
            )
        if corruption == "future_registry":
            await db.execute(
                text(
                    "UPDATE mcp_tokens SET created_at = created_at + interval '1 minute' WHERE jti = :jti"
                ),
                {"jti": jti},
            )
        if corruption == "system_label":
            from daimon.core.mcp_auth import chat_token_label

            await db.execute(
                text("UPDATE mcp_tokens SET label = :label WHERE jti = :jti"),
                {"jti": jti, "label": chat_token_label(jti)},
            )
    if corruption == "foreign_agent":
        claims["chat_agent_id"] = str(uuid.uuid4())
    if corruption == "missing_jti":
        claims.pop("jti")
    if corruption == "missing_exp":
        claims.pop("exp")
    if corruption == "extra_agent":
        claims["agent_id"] = str(agent_id)
    if corruption == "extra_internal":
        claims["internal"] = False
    if corruption == "extra_iat":
        claims["iat"] = int(now.timestamp())
    if corruption == "extra_role":
        claims["role"] = "admin"
    if corruption == "expiry_mismatch":
        claims["exp"] -= 1
    candidate = pyjwt.encode(claims, SECRET, algorithm="HS256")
    result = await DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker).verify_token(
        candidate
    )
    assert (result is not None) == (corruption is None)
    if result is not None:
        assert result.claims["chat_agent_id"] == str(agent_id)
        assert "agent_id" not in result.claims
        assert result.claims[TOKEN_JTI_CLAIM] == str(jti)
        assert result.claims["role"] == "user"


# ---- coding keys: held to the holder's admin role or ownership of the key's agent ----


class _KeyWorld:
    """A tenant with an agent, its owner, a plain member and an admin, all on Discord."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker
        self.verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)

    async def build(self) -> None:
        async with self.sessionmaker.begin() as s:
            tenant = await make_tenant(s, platform="discord")
            self.tenant_id = tenant.id
            self.agent_id = uuid.uuid4()
            self.owner = (await make_account(s, tenant=tenant)).id
            self.member = (await make_account(s, tenant=tenant)).id
            self.admin = (await make_account(s, tenant=tenant)).id
            await set_role(s, self.admin, Role.ADMIN)
            operator = await make_account(s, tenant=tenant)
            await make_cli_principal(s, tenant=tenant, account=operator)
            self.cli_operator = operator.id
            await set_owner(
                s,
                tenant_id=tenant.id,
                agent_id=self.agent_id,
                owner_account_id=self.owner,
                source="admin_assigned",
            )

    async def mint(self, account_id: uuid.UUID, *, bound: bool = False, **kwargs: object) -> str:
        async with self.sessionmaker.begin() as s:
            return await mint_agent_mcp_token(
                s,
                account_id=account_id,
                tenant_id=self.tenant_id,
                agent_id=self.agent_id,
                label="k",
                secret=SECRET,
                now=dt.datetime.now(dt.UTC),
                platform="discord" if bound else None,
                channel_id="c-1" if bound else None,
                **kwargs,  # pyright: ignore[reportArgumentType]
            )

    async def verifies(self, token: str) -> bool:
        return await self.verifier.verify_token(token) is not None

    async def reasons(self) -> list[str | None]:
        async with self.sessionmaker() as s:
            return [e.reason for e in await list_events(s, tenant_id=self.tenant_id)]


@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
async def test_an_owners_coding_key_works_and_stops_when_ownership_is_removed(
    sessionmaker: async_sessionmaker[AsyncSession], bound: bool
) -> None:
    world = _KeyWorld(sessionmaker)
    await world.build()
    token = await world.mint(world.owner, bound=bound)

    assert await world.verifies(token), "an owner's key, bound or not, works"

    async with sessionmaker.begin() as s:
        await clear_owner(s, tenant_id=world.tenant_id, agent_id=world.agent_id)
    assert not await world.verifies(token), "removing ownership stops the key on the next request"
    assert await world.reasons() == ["not_admin_or_owner"], "the refusal is audited"


async def test_a_coding_key_for_a_member_who_owns_nothing_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = _KeyWorld(sessionmaker)
    await world.build()

    assert not await world.verifies(await world.mint(world.member))
    assert not await world.verifies(await world.mint(world.member, bound=True))


async def test_an_owner_of_another_agent_cannot_use_a_key_for_this_one(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = _KeyWorld(sessionmaker)
    await world.build()
    async with sessionmaker.begin() as s:
        await set_owner(
            s,
            tenant_id=world.tenant_id,
            agent_id=uuid.uuid4(),
            owner_account_id=world.member,
            source="admin_assigned",
        )

    assert not await world.verifies(await world.mint(world.member))


@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
async def test_an_admins_coding_key_stops_when_their_stored_role_is_demoted(
    sessionmaker: async_sessionmaker[AsyncSession], bound: bool
) -> None:
    """The unbound-key gap: a demoted admin's key used to keep working until it expired."""
    world = _KeyWorld(sessionmaker)
    await world.build()
    token = await world.mint(world.admin, bound=bound)
    assert await world.verifies(token)

    async with sessionmaker.begin() as s:
        await set_role(s, world.admin, Role.USER)

    assert not await world.verifies(token)


async def test_an_admin_who_owns_nothing_keeps_a_working_key(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = _KeyWorld(sessionmaker)
    await world.build()
    assert await world.verifies(await world.mint(world.admin))


async def test_a_members_published_report_token_still_verifies(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A report's seam token is minted for its publisher, often a plain member."""
    world = _KeyWorld(sessionmaker)
    await world.build()
    assert await world.verifies(await world.mint(world.member, purpose="report"))
    assert (
        await world.verifies(await world.mint(world.member, purpose="report", bound=True)) is False
    ), "a report token that is channel-bound keeps the older admin rule"


async def test_short_internal_credentials_for_a_member_still_verify(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = _KeyWorld(sessionmaker)
    await world.build()
    chat_execution = await world.mint(world.member, chat_execution=True, ttl_days=600 / 86400)
    provider_turn = await world.mint(
        world.member, chat_execution=True, surface="provider_turn", ttl_days=600 / 86400
    )
    assert await world.verifies(chat_execution)
    assert await world.verifies(provider_turn)


async def test_in_session_chat_credentials_are_not_held_to_the_coding_key_rule(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An ordinary member's turn credential, held or not, verifies exactly as before."""
    world = _KeyWorld(sessionmaker)
    await world.build()
    now = dt.datetime.now(dt.UTC)
    async with sessionmaker.begin() as s:
        plain, _, _ = await mint_chat_mcp_token(
            s, account_id=world.member, agent_id=world.agent_id, secret=SECRET, now=now
        )
        held, _, _ = await mint_chat_mcp_token(
            s,
            account_id=world.member,
            agent_id=world.agent_id,
            secret=SECRET,
            now=now,
            hold=TurnHold(platform="discord", channel_id="c-held", client=False),
        )

    assert await world.verifies(plain)
    result = await world.verifier.verify_token(held)
    assert result is not None and result.claims["role"] == "user"


async def test_a_registered_agent_keys_own_admin_claims_are_ignored(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Admin comes only from the stored role, never from claims a hand-made key signs."""
    world = _KeyWorld(sessionmaker)
    await world.build()
    token = await world.mint(world.owner)
    claims = pyjwt.decode(token, SECRET, algorithms=["HS256"])
    forged = pyjwt.encode({**claims, "is_admin": True, "internal": True}, SECRET, algorithm="HS256")

    result = await world.verifier.verify_token(forged)

    assert result is not None, "the key keeps working, as a non-admin"
    assert "is_admin" not in result.claims and "internal" not in result.claims
    assert result.claims["role"] == "user"


async def test_would_coding_key_pass_reads_the_same_rule_without_writing(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = _KeyWorld(sessionmaker)
    await world.build()
    tokens = {
        "owner": await world.mint(world.owner),
        "admin": await world.mint(world.admin),
        "member": await world.mint(world.member),
        "report": await world.mint(world.member, purpose="report"),
        "cli": await world.mint(world.cli_operator),
    }
    async with sessionmaker() as s:
        passes = {
            name: await would_coding_key_pass(
                s,
                (
                    await get_mcp_token(
                        s, jti=uuid.UUID(pyjwt.decode(t, SECRET, algorithms=["HS256"])["jti"])
                    )
                ),  # pyright: ignore[reportArgumentType]
            )
            for name, t in tokens.items()
        }
    assert passes == {
        "owner": True,
        "admin": True,
        "member": False,
        "report": True,
        "cli": True,
    }


async def test_a_coding_key_minted_from_the_cli_keeps_working(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """`daimon mcp mint-agent-token` mints for the CLI operator's own account, which
    is neither an admin by role nor an owner: the operator still counts."""
    world = _KeyWorld(sessionmaker)
    await world.build()
    assert await world.verifies(await world.mint(world.cli_operator))
