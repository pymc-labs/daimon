"""A provider turn's Daimon MCP credential carries the turn's flags as MA's does.

MA's chat credential (`mint_chat_mcp_token`) and the per-turn `provider_turn`
credential an OpenAI or Gemini turn runs on (`mint_agent_mcp_token`) must sign
the same hold and Slack turn context claims, and the server must treat both the
same: a held one lists only the held allowlist, whoever's account it names.
"""

from __future__ import annotations

import datetime as dt
import uuid

import jwt as pyjwt
import pytest
from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.mcp_auth import (
    is_held_provider_row,
    mint_agent_mcp_token,
    mint_chat_mcp_token,
    provider_hold_refusal,
)
from daimon.core.mcp_hold import HOLD_CLAIM, held_catalogue, hold_claim
from daimon.core.session_seal import SEAL_CLAIM, parse_seal_claim
from daimon.core.stores.accounts import get_account_with_tenant, set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.mcp_tokens import get_mcp_token
from daimon.core.turn_hold import TurnHold
from daimon.testing.asgi import mcp_session
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SECRET = b"a" * 32
HOLD = TurnHold(platform="slack", channel_id="C-SYN-1", client=True)
SETTINGS = Settings(
    database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
    anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
    mcp=McpSettings(
        legacy_token_cutoff=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
        allow_legacy_tokens=True,
        jwt_secret=SecretStr(SECRET.decode()),
        public_url=HttpUrl("https://x/mcp"),
    ),
)


async def _account(
    sessionmaker: async_sessionmaker[AsyncSession], *, role: Role = Role.USER
) -> tuple[uuid.UUID, uuid.UUID]:
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="slack", workspace_id=f"T-{uuid.uuid4().hex[:8]}")
        account = await make_account(s, tenant=tenant)
        await make_platform_principal(
            s, platform="slack", external_id="U-SYN", tenant=tenant, account=account
        )
        if role is not Role.USER:
            await set_role(s, account.id, role)
    return tenant.id, account.id


async def _provider_token(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    hold: TurnHold | None = HOLD,
    slack_turn_context_id: uuid.UUID | None = None,
    seal_ids: frozenset[str] = frozenset(),
) -> str:
    async with sessionmaker() as s, s.begin():
        return await mint_agent_mcp_token(
            s,
            account_id=account_id,
            tenant_id=tenant_id,
            agent_id=uuid.uuid4(),
            label="provider-turn",
            secret=SECRET,
            now=dt.datetime.now(dt.UTC),
            ttl_days=600 / 86400,  # pyright: ignore[reportArgumentType]
            chat_execution=True,
            surface="provider_turn",
            hold=hold,
            slack_turn_context_id=slack_turn_context_id,
            seal_ids=seal_ids,
        )


async def _chat_token(
    sessionmaker: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    *,
    hold: TurnHold | None = HOLD,
    slack_turn_context_id: uuid.UUID | None = None,
) -> str:
    async with sessionmaker() as s, s.begin():
        token, _, _ = await mint_chat_mcp_token(
            s,
            account_id=account_id,
            agent_id=uuid.uuid4(),
            secret=SECRET,
            now=dt.datetime.now(dt.UTC),
            hold=hold,
            slack_turn_context_id=slack_turn_context_id,
        )
    return token


def _claims(token: str) -> dict[str, object]:
    return pyjwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_exp": False})


def _resign(token: str, key: str, value: object | None) -> str:
    claims = _claims(token)
    if value is None:
        claims.pop(key, None)
    else:
        claims[key] = value
    return pyjwt.encode(claims, SECRET, algorithm="HS256")


@pytest.mark.parametrize("held", [True, False])
@pytest.mark.parametrize("slack_context", [True, False])
async def test_ma_and_provider_credentials_sign_the_same_turn_flags(
    sessionmaker: async_sessionmaker[AsyncSession], held: bool, slack_context: bool
) -> None:
    tenant_id, account_id = await _account(sessionmaker)
    hold = HOLD if held else None
    context = uuid.uuid4() if slack_context else None
    ma = _chat_token(sessionmaker, account_id, hold=hold, slack_turn_context_id=context)
    provider = _provider_token(
        sessionmaker, tenant_id, account_id, hold=hold, slack_turn_context_id=context
    )
    ma, provider = _claims(await ma), _claims(await provider)

    for key in (HOLD_CLAIM, "slack_turn_context_id"):
        assert ma.get(key) == provider.get(key), f"{key} differs between MA and provider"
    assert (HOLD_CLAIM in provider) is held, "the hold claim follows the turn's hold"


async def test_a_held_provider_row_is_bound_to_its_channel(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _account(sessionmaker)
    token = await _provider_token(sessionmaker, tenant_id, account_id)
    async with sessionmaker() as s:
        row = await get_mcp_token(s, jti=uuid.UUID(str(_claims(token)["jti"])))
    assert row is not None, "the credential is registered"
    assert is_held_provider_row(row), "a held provider credential carries the held label"
    assert (row.platform, row.channel_id) == ("slack", "C-SYN-1"), "the row names the channel"
    assert provider_hold_refusal(row, hold_claim(HOLD)) is None, "its own claim matches"
    other = hold_claim(TurnHold("slack", "C-SYN-2", client=True))
    assert provider_hold_refusal(row, other) == "hold_binding_mismatch", "another channel fails"


async def test_a_hold_needs_the_provider_turn_surface(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _account(sessionmaker)
    async with sessionmaker() as s, s.begin():
        with pytest.raises(ValueError, match="provider-turn"):
            await mint_agent_mcp_token(
                s,
                account_id=account_id,
                tenant_id=tenant_id,
                agent_id=uuid.uuid4(),
                label="key",
                secret=SECRET,
                now=dt.datetime.now(dt.UTC),
                hold=HOLD,
            )


@pytest.mark.parametrize("slack_context", [True, False])
async def test_a_held_provider_credential_of_a_non_admin_verifies(
    sessionmaker: async_sessionmaker[AsyncSession], slack_context: bool
) -> None:
    """Its channel binding is the hold's, not an admin's channel-scoped key."""
    tenant_id, account_id = await _account(sessionmaker, role=Role.USER)
    context = uuid.uuid4() if slack_context else None
    token = await _provider_token(
        sessionmaker, tenant_id, account_id, slack_turn_context_id=context
    )

    access = await DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker).verify_token(token)

    assert access is not None, "a held provider-turn credential verifies"
    assert access.claims[HOLD_CLAIM] == hold_claim(HOLD), "the server reads the turn's hold"
    assert access.claims.get("slack_turn_context_id") == (
        None if context is None else str(context)
    ), "the Slack turn context reaches the server"


@pytest.mark.parametrize("held", [True, False])
async def test_a_sealed_provider_credential_signs_and_verifies_its_seals(
    sessionmaker: async_sessionmaker[AsyncSession], held: bool
) -> None:
    tenant_id, account_id = await _account(sessionmaker)
    seals = frozenset({"C-SYN-1", "C-SYN-1:1700000000.0001"})
    token = await _provider_token(
        sessionmaker, tenant_id, account_id, hold=HOLD if held else None, seal_ids=seals
    )
    assert parse_seal_claim(_claims(token)[SEAL_CLAIM]) == seals, "the turn's seals are signed"

    access = await DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker).verify_token(token)

    assert access is not None, "a sealed provider-turn credential verifies"
    assert parse_seal_claim(access.claims[SEAL_CLAIM]) == seals, "the server reads the seals"


async def test_a_sealed_self_hosted_chat_credential_signs_its_seals(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _, account_id = await _account(sessionmaker)
    async with sessionmaker() as s, s.begin():
        token, _, _ = await mint_chat_mcp_token(
            s,
            account_id=account_id,
            agent_id=uuid.uuid4(),
            secret=SECRET,
            now=dt.datetime.now(dt.UTC),
            seal_ids=frozenset({"C-SYN-1"}),
        )

    access = await DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker).verify_token(token)

    assert access is not None, "a sealed self-hosted turn credential verifies"
    assert parse_seal_claim(access.claims[SEAL_CLAIM]) == {"C-SYN-1"}, "its seals arrive"


async def test_an_unheld_slack_context_provider_credential_verifies(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _account(sessionmaker)
    context = uuid.uuid4()
    token = await _provider_token(
        sessionmaker, tenant_id, account_id, hold=None, slack_turn_context_id=context
    )

    access = await DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker).verify_token(token)

    assert access is not None, "a Slack-context provider-turn credential verifies"
    assert access.claims.get("slack_turn_context_id") == str(context), "its context arrives"


async def test_seals_need_the_provider_turn_surface(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _account(sessionmaker)
    async with sessionmaker() as s, s.begin():
        with pytest.raises(ValueError, match="provider-turn"):
            await mint_agent_mcp_token(
                s,
                account_id=account_id,
                tenant_id=tenant_id,
                agent_id=uuid.uuid4(),
                label="key",
                secret=SECRET,
                now=dt.datetime.now(dt.UTC),
                seal_ids=frozenset({"C-SYN-1"}),
            )


@pytest.mark.parametrize("value", [[], "C-SYN-1", [""], [1], {"C-SYN-1": True}])
def test_a_malformed_seal_claim_is_refused(value: object) -> None:
    assert parse_seal_claim(value) is None, "a malformed seal claim never reads as unsealed"
    assert parse_seal_claim(None) == frozenset(), "no claim is no seal"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(hold_claim(TurnHold("slack", "C-SYN-2", client=True)), id="other-channel"),
        pytest.param(None, id="missing"),
        pytest.param({"platform": "slack"}, id="malformed"),
    ],
)
async def test_a_held_provider_credential_without_its_claim_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession], value: object | None
) -> None:
    tenant_id, account_id = await _account(sessionmaker)
    token = _resign(await _provider_token(sessionmaker, tenant_id, account_id), HOLD_CLAIM, value)
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    assert await verifier.verify_token(token) is None, "the row and claim must agree"


async def test_a_hold_claim_on_an_unheld_provider_row_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _account(sessionmaker)
    unheld = await _provider_token(sessionmaker, tenant_id, account_id, hold=None)
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)
    assert await verifier.verify_token(unheld) is not None, "the unheld credential verifies"
    forged = _resign(unheld, HOLD_CLAIM, hold_claim(HOLD))
    assert await verifier.verify_token(forged) is None, "a claim cannot hold an unheld row"


@pytest.mark.usefixtures("platform_admin_edge")
async def test_a_held_provider_turn_lists_the_held_part_of_its_catalogue(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """What the host expects of a held turn (`held_catalogue`) is what the server lists."""
    tenant_id, account_id = await _account(sessionmaker, role=Role.ADMIN)
    async with sessionmaker() as s:
        identity = await get_account_with_tenant(s, account_id=account_id)
    assert identity is not None and identity.tenant_id == tenant_id, "fixture tenant"
    app = create_mcp_app(settings=SETTINGS, sessionmaker=sessionmaker)

    async def listed(token: str) -> frozenset[str]:
        result = await mcp_session(app, token=token, method="tools/list")
        return frozenset(t["name"] for t in result["result"]["tools"])  # type: ignore[index]

    unheld = await listed(await _provider_token(sessionmaker, tenant_id, account_id, hold=None))
    held = await listed(await _provider_token(sessionmaker, tenant_id, account_id))
    assert held == held_catalogue(unheld), "a held turn lists allowlisted captured tools only"
    assert held < unheld, "the hold narrows the catalogue"
