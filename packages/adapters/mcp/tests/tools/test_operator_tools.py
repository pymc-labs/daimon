"""Operator-token tools: the tenant summary, promo issuing under a ceiling, and the gates."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
from daimon.adapters.mcp.middleware.mcp_identity import (
    IdentityMiddleware,
    production_agent_id_resolver,
    production_internal_resolver,
    production_is_admin_resolver,
    production_role_resolver,
    production_subject_resolver,
    production_tenant_resolver,
)
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _admit,  # pyright: ignore[reportPrivateUsage]
    _auth,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._pin_guard import (
    _trusted_credential,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.channel_admins import (
    _clear_channel_admins_impl,  # pyright: ignore[reportPrivateUsage]
    _list_channel_admins_impl,  # pyright: ignore[reportPrivateUsage]
    _set_channel_admins_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.promo_issuing import (
    _create_promo_code_impl,  # pyright: ignore[reportPrivateUsage]
    _list_promo_codes_impl,  # pyright: ignore[reportPrivateUsage]
    _revoke_promo_code_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.propagation import (
    _clear_agent_default_impl,  # pyright: ignore[reportPrivateUsage]
    _set_agent_default_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.tenant_summary import (
    ChannelAdmins,
    _get_tenant_summary_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.mcp_auth import mint_operator_mcp_token
from daimon.core.operator_tokens import OperatorScope, scope_tag
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores import promo_codes
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.direct_messages import DirectMessageRow, start_conversation
from daimon.core.stores.domain import Role, TenantRow
from daimon.core.stores.mcp_tokens import create_mcp_token_row, get_mcp_token, revoke_mcp_token
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.factories import (
    make_account,
    make_channel_budget,
    make_ledger_entry,
    make_tenant,
    make_tenant_config,
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import seed_server_admin


def _runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        ),
        deployment_default=DeploymentDefault(environment_name="default-env"),
    )


async def _operator(
    sessionmaker: async_sessionmaker[AsyncSession],
    *scopes: OperatorScope,
    max_issued_usd: Decimal | None = None,
) -> tuple[TenantRow, AuthIdentity]:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        jti = uuid.uuid4()
        await create_mcp_token_row(
            session,
            jti=jti,
            account_id=account.id,
            tenant_id=tenant.id,
            agent_id=None,
            kind="operator",
            scopes=set(scopes),
            label=None,
            created_at=datetime.now(UTC),
            max_issued_usd=max_issued_usd,
        )
    return tenant, _identity(tenant.id, account.id, jti=jti, scopes=frozenset(scopes))


def _identity(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    jti: uuid.UUID | None = None,
    scopes: frozenset[str] = frozenset(),
) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id,
        tenant_id=tenant_id,
        role=Role.ADMIN,
        platform="discord",
        platform_user_id="u-admin",
        is_admin=True,
        token_kind="operator" if jti is not None else None,
        token_jti=jti,
        scopes=scopes,
    )


async def test_tenant_summary_lists_configured_and_budgeted_channels(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant, auth = await _operator(committing_sessionmaker, "tenant:read")
    async with committing_sessionmaker.begin() as session:
        await make_ledger_entry(session, tenant=tenant, delta_usd=Decimal("12.5"))
        await make_tenant_config(session, tenant=tenant, agent_name="helper")
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"),
            tenant_id=tenant.id,
            environment_name="data-env",
        )
        await make_channel_budget(session, tenant=tenant, channel_id="c2", limit_usd=Decimal(5))

    summary = await _get_tenant_summary_impl(_runtime(committing_sessionmaker), auth)

    assert (summary.balance_usd, summary.funding_mode, summary.default_agent) == (
        "12.50",
        tenant.funding_mode,
        "helper",
    ), "the tenant's balance, funding mode and default agent"
    assert [
        (c.channel_id, c.agent_name, c.environment_name, c.isolated) for c in summary.channels
    ] == [("c1", "helper", "data-env", False), ("c2", "helper", "default-env", False)], (
        "every configured or budgeted channel, with what resolves there"
    )
    assert summary.channels[0].budget is None, "c1 has no budget"
    budget = summary.channels[1].budget
    assert budget is not None and (budget.limit_usd, budget.window) == ("5.00", "monthly")
    assert summary.channels[0].admins == ChannelAdmins(role_ids=[], user_ids=[])


async def test_tenant_summary_lists_each_channels_admins(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A channel with admins and nothing else is listed too."""
    tenant, auth = await _operator(committing_sessionmaker, "tenant:read")
    async with committing_sessionmaker.begin() as session:
        await make_channel_budget(session, tenant=tenant, channel_id="c1", limit_usd=Decimal(5))
        await set_channel_admins(
            session,
            tenant_id=tenant.id,
            platform=tenant.platform,
            channel_id="c2",
            role_ids=["r1"],
            user_ids=["u2", "u1"],
            actor_account_id=auth.account_id,
        )

    summary = await _get_tenant_summary_impl(_runtime(committing_sessionmaker), auth)

    assert [(c.channel_id, c.admins) for c in summary.channels] == [
        ("c1", ChannelAdmins(role_ids=[], user_ids=[])),
        ("c2", ChannelAdmins(role_ids=["r1"], user_ids=["u1", "u2"])),
    ], "each channel carries its stored admins, none where nobody was named"
    assert summary.channels[1].budget is None, "c2 is listed for its admins alone"


async def test_tenant_summary_leaves_out_dm_channels(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A DM's channel gets a config row and a dm: binding when it starts, live or since moved."""
    tenant, auth = await _operator(committing_sessionmaker, "tenant:read")
    async with committing_sessionmaker.begin() as session:
        for channel_id in ("c1", "dm-live", "dm-moved"):
            await set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=channel_id),
                tenant_id=tenant.id,
                agent_name="helper",
            )
        for channel_id in ("dm-live", "dm-moved"):
            await create_binding(
                session,
                tenant_id=tenant.id,
                platform="discord",
                parent_channel_id=channel_id,
                thread_id=f"dm:{uuid.uuid4()}",
                responder_ma_agent_id="agent_1",
                responder_name="helper",
                kind="handoff",
            )
        await start_conversation(
            session,
            conversation=DirectMessageRow(
                platform="discord",
                route_key="dm-live",
                external_user_id="u-admin",
                tenant_id=tenant.id,
                account_id=auth.account_id,
                workspace_id=tenant.external_id,
                channel_id="dm-live",
                scope_id="dm:live",
                source_url="https://example.com/c1",
                context="",
                memory_read_only=False,
                history=[],
                recent_message_ids=[],
                active_until=None,
            ),
            now=datetime.now(UTC),
        )

    summary = await _get_tenant_summary_impl(_runtime(committing_sessionmaker), auth)

    assert [c.channel_id for c in summary.channels] == ["c1"], "DM channels are not listed"


async def test_operator_without_the_scope_is_refused_by_the_tool_itself(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _tenant, auth = await _operator(committing_sessionmaker, "promo:redeem")
    with pytest.raises(ToolError, match="does not have the tenant:read scope"):
        await _get_tenant_summary_impl(_runtime(committing_sessionmaker), auth)


async def test_agent_default_tools_require_channels_write(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _tenant, auth = await _operator(committing_sessionmaker, "tenant:read")
    runtime = _runtime(committing_sessionmaker)
    with pytest.raises(ToolError, match="does not have the channels:write scope"):
        await _set_agent_default_impl(runtime, auth, "helper", "c1")
    with pytest.raises(ToolError, match="does not have the channels:write scope"):
        await _clear_agent_default_impl(runtime, auth, "c1")


async def test_channel_admin_tools_require_their_scopes(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _runtime(committing_sessionmaker)
    _tenant, reader = await _operator(committing_sessionmaker, "tenant:read")
    _tenant, writer = await _operator(committing_sessionmaker, "channels:write")
    with pytest.raises(ToolError, match="does not have the channels:write scope"):
        await _set_channel_admins_impl(
            runtime, reader, channel_id="111111111111111111", role_ids=[], user_ids=[]
        )
    with pytest.raises(ToolError, match="does not have the channels:write scope"):
        await _clear_channel_admins_impl(runtime, reader, channel_id="111111111111111111")
    with pytest.raises(ToolError, match="does not have the tenant:read scope"):
        await _list_channel_admins_impl(runtime, writer)


async def test_server_admin_cannot_create_promo_codes(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant, operator = await _operator(committing_sessionmaker, "promo:create")
    admin = _identity(tenant.id, operator.account_id)
    with pytest.raises(ToolError, match="Only an operator token with the promo:create scope"):
        await _create_promo_code_impl(
            _runtime(committing_sessionmaker), admin, amount_usd="5", kind="credit"
        )


async def test_create_promo_code_counts_against_the_ceiling(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _tenant, auth = await _operator(
        committing_sessionmaker, "promo:create", max_issued_usd=Decimal(100)
    )
    runtime = _runtime(committing_sessionmaker)

    created = await _create_promo_code_impl(
        runtime, auth, amount_usd="20", kind="credit", max_redemptions=3, note="launch"
    )
    with pytest.raises(ToolError, match=r"could grant \$60.00, more than the \$40.00 left"):
        await _create_promo_code_impl(
            runtime, auth, amount_usd="20", kind="credit", max_redemptions=3
        )
    with pytest.raises(ToolError, match="max_redemptions is required"):
        await _create_promo_code_impl(runtime, auth, amount_usd="1", kind="credit")

    assert (created.amount_usd, created.max_redemptions, created.ceiling_remaining_usd) == (
        "20.00",
        3,
        "40.00",
    ), "the result reports what the token may still issue"
    assert created.code, "the code is returned once"
    async with committing_sessionmaker() as session:
        token = await get_mcp_token(session, jti=auth.token_jti or uuid.uuid4())
        codes = await promo_codes.list_promo_codes(session)
    assert token is not None and token.issued_usd == Decimal(60), "issued credit is tracked"
    assert [str(code.id) for code in codes] == [created.promo_code_id], (
        "a refused code is never stored"
    )


async def test_create_promo_code_refuses_a_token_revoked_after_it_was_verified(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _tenant, auth = await _operator(committing_sessionmaker, "promo:create")
    async with committing_sessionmaker.begin() as session:
        await revoke_mcp_token(session, jti=auth.token_jti or uuid.uuid4(), now=datetime.now(UTC))

    with pytest.raises(ToolError, match="was revoked. Nothing was created"):
        await _create_promo_code_impl(
            _runtime(committing_sessionmaker), auth, amount_usd="5", kind="credit"
        )

    async with committing_sessionmaker() as session:
        codes = await promo_codes.list_promo_codes(session)
    assert codes == [], "a revoked token issues nothing"


async def test_create_promo_code_without_a_ceiling_is_untracked(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _tenant, auth = await _operator(committing_sessionmaker, "promo:create")
    created = await _create_promo_code_impl(
        _runtime(committing_sessionmaker), auth, amount_usd="5", kind="credit"
    )
    assert created.ceiling_remaining_usd is None, "no ceiling, nothing to report"


async def test_list_and_revoke_promo_codes(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _tenant, auth = await _operator(committing_sessionmaker, "promo:create")
    runtime = _runtime(committing_sessionmaker)
    created = await _create_promo_code_impl(runtime, auth, amount_usd="5", kind="credit")

    revoked = await _revoke_promo_code_impl(runtime, auth, created.promo_code_id)
    listed = await _list_promo_codes_impl(runtime, auth)

    assert revoked.revoked_at is not None, "revoking stamps the code"
    assert [(code.promo_code_id, code.revoked_at) for code in listed] == [
        (created.promo_code_id, revoked.revoked_at)
    ], "the list shows the revoked code"
    with pytest.raises(ToolError, match="no promo code"):
        await _revoke_promo_code_impl(runtime, auth, str(uuid.uuid4()))


async def _verified_operator_identity(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AuthIdentity:
    """The identity a tool sees for a real operator token of a server admin.

    Mints the token, then sends a call through the real verifier and identity
    middleware, so nothing about the identity is assumed by the test.
    """
    secret = b"a" * 32
    async with sessionmaker() as s, s.begin():
        tenant_id, account_id = await seed_server_admin(s)
        token = await mint_operator_mcp_token(
            s,
            account_id=account_id,
            tenant_id=tenant_id,
            scopes=frozenset({"tenant:read"}),
            label=None,
            secret=secret,
            now=datetime.now(UTC),
            ttl_days=30,
        )
    middleware = IdentityMiddleware(
        subject_resolver=production_subject_resolver,
        tenant_resolver=production_tenant_resolver,
        role_resolver=production_role_resolver,
        agent_id_resolver=production_agent_id_resolver,
        is_admin_resolver=production_is_admin_resolver,
        internal_resolver=production_internal_resolver,
        sessionmaker=sessionmaker,
    )
    mcp = FastMCP(name="identity", auth=DaimonJWTVerifier(secret=secret, sessionmaker=sessionmaker))
    mcp.add_middleware(middleware)
    captured: list[AuthIdentity] = []

    @mcp.tool(tags={scope_tag("tenant:read")})
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        captured.append(await _auth(ctx))
        return "ok"

    await call_mcp_tool(mcp.http_app(), token=token, name="whoami")
    await middleware.drain_audit()
    assert len(captured) == 1, "the operator token reached the tool"
    assert captured[0].is_operator, "the verifier marked it an operator token"
    return captured[0]


async def test_pin_guard_never_trusts_an_operator_token_as_the_deployment_operator(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    operator = await _verified_operator_identity(sessionmaker)
    assert _trusted_credential(operator) is False, (
        "an operator token carries a platform user, so it is not the unbilled operator path"
    )


async def test_admit_bills_an_operator_token(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    operator = await _verified_operator_identity(sessionmaker)
    with pytest.raises(ToolError, match="credit is depleted"):
        await _admit(operator, sessionmaker=sessionmaker, billing_config=None, tool_name="ask")
