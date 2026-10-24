"""Stale internal claims cannot survive revocation or become platform authority."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.middleware.mcp_identity import IdentityMiddleware
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.admins import change_admin
from daimon.adapters.mcp.tools.channel_budgets import require_budget_write
from daimon.adapters.mcp.tools.promo_codes import _redeem_promo_code_impl
from daimon.core.admin_actions import DelegatedAction
from daimon.core.config import DiscordSettings
from daimon.core.discord_membership import DiscordRestMembershipResolver
from daimon.core.membership import Membership
from daimon.core.stores.accounts import set_role
from daimon.core.stores.daimon_admins import change_grant, list_admins
from daimon.core.stores.domain import McpTokenKind, Role
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_platform_principal, make_tenant
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_mcp_refuses_stale_admin_claim_after_revoke(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session, workspace_id="123456789012345678")
    principal = await make_platform_principal(
        db_session, tenant=tenant, platform="discord", external_id="223456789012345678"
    )
    key = dict(
        tenant_id=tenant.id,
        platform="discord",
        subject_kind="user",
        subject_id=principal.external_id,
        actor_user_id="owner",
        is_platform_admin=True,
    )
    await change_grant(db_session, **key)
    await set_role(db_session, principal.account_id, Role.ADMIN)
    await db_session.commit()
    membership = AsyncMock(return_value=Membership(is_member=True))
    monkeypatch.setattr(DiscordRestMembershipResolver, "resolve", membership)
    settings = MagicMock(discord=DiscordSettings(bot_token=SecretStr("test")))
    mcp = FastMCP(name="delegation test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=AsyncMock(return_value=str(principal.account_id)),
            tenant_resolver=AsyncMock(return_value=str(tenant.id)),
            role_resolver=AsyncMock(return_value="admin"),
            agent_id_resolver=AsyncMock(return_value=None),
            is_admin_resolver=AsyncMock(return_value="true"),
            internal_resolver=AsyncMock(return_value="true"),
            sessionmaker=db_session_factory,
            settings=settings,
        )
    )
    captured: list[AuthIdentity] = []

    @mcp.tool
    async def inspect_authority(ctx: Context) -> bool:
        auth = await ctx.get_state("auth")
        assert isinstance(auth, AuthIdentity)
        captured.append(auth)
        return auth.is_admin

    async with Client(mcp) as client:
        await client.call_tool("inspect_authority", {})
        assert captured[-1].is_admin and not captured[-1].is_platform_admin
        await change_grant(db_session, **key, revoke=True)
        await db_session.commit()
        await client.call_tool("inspect_authority", {})
        assert not captured[-1].is_admin
        assert captured[-1].role is Role.USER, (
            "neither a stale DB role claim nor internal claim elevates"
        )
        membership.return_value = Membership(is_member=True, is_admin=True)
        await client.call_tool("inspect_authority", {})
        assert captured[-1].is_admin and captured[-1].is_platform_admin, (
            "revoking a grant and demoting its account cannot revoke live platform authority"
        )
        membership.return_value = Membership()
        await client.call_tool("inspect_authority", {})
        assert not captured[-1].is_admin and not captured[-1].is_platform_admin


@pytest.mark.parametrize("revoke", [False, True])
async def test_granted_admin_cannot_write_delegations_over_mcp(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    revoke: bool,
) -> None:
    tenant = await make_tenant(db_session)
    principal = await make_platform_principal(
        db_session, tenant=tenant, platform="discord", external_id="delegate"
    )
    await change_grant(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        subject_kind="user",
        subject_id="delegate",
        actor_user_id="owner",
        is_platform_admin=True,
    )
    await db_session.commit()
    monkeypatch.setattr(
        DiscordRestMembershipResolver, "resolve", AsyncMock(return_value=Membership(is_member=True))
    )
    runtime = MagicMock(spec=McpRuntime, session_factory=db_session_factory)
    runtime.settings = MagicMock(discord=DiscordSettings(bot_token=SecretStr("test")))
    auth = AuthIdentity(
        account_id=principal.account_id,
        tenant_id=tenant.id,
        platform="discord",
        platform_user_id="delegate",
        token_kind="cli",
        role=Role.ADMIN,
        is_admin=True,
    )
    with pytest.raises(ToolError, match="Only platform admins"):
        await change_admin(runtime, auth, subject_id="new-admin", revoke=revoke)
    assert len(await list_admins(db_session, tenant_id=tenant.id)) == 1
    assert len(await list_events(db_session, tenant_id=tenant.id)) == 1


@pytest.mark.parametrize("token_kind", [None, "agent"])
async def test_delegated_admin_is_refused_at_money_gates(token_kind: McpTokenKind | None) -> None:
    import uuid

    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.ADMIN,
        is_admin=True,
        token_kind=token_kind,
    )
    with pytest.raises(ToolError, match="admin"):
        require_budget_write(auth, "channel")
    runtime = MagicMock(spec=McpRuntime, session_factory=MagicMock())
    with pytest.raises(ToolError, match="platform admin"):
        await _redeem_promo_code_impl(runtime, auth, "CODE")
    runtime.session_factory.assert_not_called()


async def test_in_flight_delegation_recheck_does_not_reuse_an_admin_snapshot(
    db_session: AsyncSession,
) -> None:
    import uuid

    from daimon.adapters.mcp.tools._admin import current_admin

    tenant = await make_tenant(db_session)
    key = dict(
        tenant_id=tenant.id,
        platform="discord",
        subject_kind="role",
        subject_id="role",
        actor_user_id="owner",
        is_platform_admin=True,
    )
    await change_grant(db_session, **key)
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant.id,
        platform="discord",
        platform_user_id="member",
        role=Role.ADMIN,
        is_admin=True,
        verified_role_ids=("role",),
    )
    assert await current_admin(db_session, auth, action=DelegatedAction.AGENT_SETTINGS)
    await change_grant(db_session, **key, revoke=True)
    assert not await current_admin(db_session, auth, action=DelegatedAction.AGENT_SETTINGS), (
        "a request-local admin bit cannot resurrect a revoked grant"
    )
