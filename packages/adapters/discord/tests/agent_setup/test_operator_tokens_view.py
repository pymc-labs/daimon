"""Operator tokens screen: server admins mint, list and revoke; members are refused."""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.agent_setup.operator_tokens_view import (
    MINT_LABEL,
    MintOperatorTokenModal,
    OperatorTokensView,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import Role
from daimon.core.stores.mcp_tokens import list_mcp_tokens
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_stub_anthropic
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD_ID = 2001


def _runtime(sessionmaker: object) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp.jwt_secret = SecretStr("jwt-secret")
    return DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=sessionmaker,  # type: ignore[arg-type]
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]
    )


def _interaction(*, admin: bool) -> MagicMock:
    interaction = MagicMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 42
    interaction.user.guild_permissions.administrator = admin
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild.owner_id = 999
    acked: list[str] = []
    interaction.response.is_done = MagicMock(side_effect=lambda: bool(acked))
    for name in ("send_message", "send_modal", "edit_message", "defer"):
        setattr(
            interaction.response,
            name,
            AsyncMock(side_effect=lambda *_, n=name, **__: acked.append(n)),
        )
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


async def _view(factory: async_sessionmaker[AsyncSession]) -> OperatorTokensView:
    async with factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
        account = await make_account(session, tenant=tenant)
    state = PanelState(
        roster=[], selected=None, account_id=account.id, is_admin=True, guild_id=GUILD_ID
    )
    return OperatorTokensView(state, runtime=_runtime(factory), allowed_user_id=42, rows=[])


def _modal(view: OperatorTokensView, scopes: list[str]) -> MintOperatorTokenModal:
    modal = MintOperatorTokenModal(view)
    modal.scopes._values = scopes  # pyright: ignore[reportPrivateUsage]
    modal.label._value = "ci"  # pyright: ignore[reportPrivateUsage]
    return modal


def _walk(item: Any) -> list[Any]:
    return [item] + [n for c in getattr(item, "children", []) or [] for n in _walk(c)]


async def _audited(factory: async_sessionmaker[AsyncSession]) -> list[tuple[str, str]]:
    async with factory() as session:
        rows = await list_events(
            session, tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(GUILD_ID))
        )
    return [(row.tool_name, row.outcome) for row in rows]


async def test_an_admin_mints_a_tenant_token_shown_once_then_revokes_it(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    view = await _view(db_session_factory)
    admin = _interaction(admin=True)
    await _modal(view, ["tenant:read", "channels:write"]).on_submit(admin)

    async with db_session_factory() as session:
        (row,) = await list_mcp_tokens(session, now=dt.datetime.now(dt.UTC), kind="operator")
        identity = await get_account_with_tenant(session, account_id=row.account_id)
    assert set(row.scopes) == {"tenant:read", "channels:write"} and row.label == "ci"
    assert identity is not None and identity.role is Role.ADMIN, (
        "the live admin check is stored, so the verifier admits the token"
    )
    shown = admin.followup.send.call_args
    assert shown.kwargs == {"ephemeral": True} and "one time" in shown.args[0]
    screen = admin.edit_original_response.call_args.kwargs["view"]
    assert str(row.jti)[:8] in "".join(
        str(n.content) for n in _walk(screen) if isinstance(n, discord.ui.TextDisplay)
    ), "the listing shows the new token's short id, never the token"

    revoke = next(n for n in _walk(screen) if isinstance(n, discord.ui.Select))
    revoke._values = [str(row.jti)]  # pyright: ignore[reportPrivateUsage]
    await revoke.callback(_interaction(admin=True))
    async with db_session_factory() as session:
        assert await list_mcp_tokens(session, now=dt.datetime.now(dt.UTC), kind="operator") == []
    assert await _audited(db_session_factory) == [
        ("panel:operator_token_mint", "allowed"),
        ("panel:operator_token_revoke", "allowed"),
    ]


async def test_the_token_is_shown_even_when_the_panel_swap_fails(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    view = await _view(db_session_factory)
    admin = _interaction(admin=True)
    admin.edit_original_response.side_effect = discord.NotFound(MagicMock(status=404), "gone")
    with pytest.raises(discord.NotFound):
        await _modal(view, ["tenant:read"]).on_submit(admin)
    shown = admin.followup.send.call_args
    assert shown is not None and "one time" in shown.args[0], (
        "the minted token reaches the admin before the swap that failed"
    )


async def test_a_member_cannot_mint_or_open_the_mint_form(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    view = await _view(db_session_factory)
    member = _interaction(admin=False)
    button = next(
        n for n in _walk(view) if isinstance(n, discord.ui.Button) and n.label == MINT_LABEL
    )
    await button.callback(member)
    member.response.send_modal.assert_not_awaited()
    await _modal(view, ["tenant:read"]).on_submit(_interaction(admin=False))

    async with db_session_factory() as session:
        assert await list_mcp_tokens(session, now=dt.datetime.now(dt.UTC)) == []
    assert await _audited(db_session_factory) == [
        ("panel:operator_token_mint", "denied"),
        ("panel:operator_token_mint", "denied"),
    ]


async def test_the_panel_never_mints_the_deployment_promo_scope(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    view = await _view(db_session_factory)
    admin = _interaction(admin=True)
    await _modal(view, ["promo:create"]).on_submit(admin)
    assert "mint-operator-token" in admin.response.send_message.call_args.args[0]
    async with db_session_factory() as session:
        assert await list_mcp_tokens(session, now=dt.datetime.now(dt.UTC)) == []
