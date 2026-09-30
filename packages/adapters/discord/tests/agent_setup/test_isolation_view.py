"""The isolation screen: what it offers, who may click, and what a click stores."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.agent_setup.isolation_view import (
    END_LABEL,
    ISOLATE_COPY_LABEL,
    ISOLATE_LABEL,
    IsolationView,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import FakeMAState, build_fake_anthropic, make_fake_ma_handler
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD_ID = 2001
CHANNEL_ID = 900000000000000001


def _runtime(sessionmaker: object, state: FakeMAState | None = None) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp.public_url = None
    settings.github.oauth_scopes = ()
    settings.crypto.keys = ()
    return DiscordRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(make_fake_ma_handler(state or FakeMAState())),
        sessionmaker=sessionmaker,  # type: ignore[arg-type]
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(agent_name="daimon"),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]
    )


def _view(runtime: DiscordRuntime, account_id: uuid.UUID, *, isolated: bool) -> IsolationView:
    state = PanelState(
        roster=[],
        selected=None,
        account_id=account_id,
        is_admin=True,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        channel_name="Team Alpha",
    )
    return IsolationView(state, runtime=runtime, allowed_user_id=42, isolated=isolated)


def _interaction(*, admin: bool = True) -> MagicMock:
    interaction = MagicMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 42
    interaction.user.guild_permissions.administrator = admin
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild.owner_id = 999
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    return interaction


def _walk(item: Any) -> list[Any]:
    found = [item]
    for child in getattr(item, "children", []) or []:
        found.extend(_walk(child))
    return found


def _labels(view: discord.ui.LayoutView) -> set[str]:
    return {node.label for node in _walk(view) if isinstance(node, discord.ui.Button)}


def _button(view: discord.ui.LayoutView, label: str) -> Any:
    return next(n for n in _walk(view) if isinstance(n, discord.ui.Button) and n.label == label)


def _text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        str(node.content) for node in _walk(view) if isinstance(node, discord.ui.TextDisplay)
    )


def test_screen_offers_isolating_or_ending_it(account_id: uuid.UUID) -> None:
    runtime = _runtime(MagicMock())
    assert _labels(_view(runtime, account_id, isolated=False)) == {
        "◀ Back",
        ISOLATE_LABEL,
        ISOLATE_COPY_LABEL,
        "Done",
    }, "an open channel can be isolated, with or without a copy"
    assert _labels(_view(runtime, account_id, isolated=True)) == {"◀ Back", END_LABEL, "Done"}


async def test_a_member_who_lost_manage_server_changes_nothing(account_id: uuid.UUID) -> None:
    sessionmaker = MagicMock()
    view = _view(_runtime(sessionmaker), account_id, isolated=False)
    member = _interaction(admin=False)
    await _button(view, ISOLATE_LABEL).callback(member)
    member.response.send_message.assert_awaited_once()
    sessionmaker.begin.assert_not_called()


async def test_isolate_refuses_a_shared_agent_then_copies_it_and_ends(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
        account = await make_account(session, tenant=tenant)
        for channel in (str(CHANNEL_ID), "900000000000000002"):
            await set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=channel),
                tenant_id=tenant.id,
                agent_name="shared",
                mode="agent",
            )
    state = FakeMAState()
    agent = ma_agent(id="agent_shared", name="shared", tenant_id=tenant.id)
    state.agents[agent.id] = agent.model_dump(mode="json")
    runtime = _runtime(db_session_factory, state)

    refused = _interaction()
    await _button(_view(runtime, account.id, isolated=False), ISOLATE_LABEL).callback(refused)
    screen = refused.response.edit_message.call_args.kwargs["view"]
    assert "also answers outside this channel" in _text(screen), "the refusal says why"
    assert END_LABEL not in _labels(screen), "nothing was isolated"

    copied = _interaction()
    await _button(screen, ISOLATE_COPY_LABEL).callback(copied)
    screen = copied.response.edit_message.call_args.kwargs["view"]
    assert "**team-alpha**, a copy of **shared**" in _text(screen)
    async with db_session_factory() as session:
        scope = await get_scope(
            session, scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=str(CHANNEL_ID))
        )
        policy = await load_access_policy(session, tenant_id=tenant.id)
    assert scope is not None and scope.agent_name == "team-alpha", "the copy answers here"
    assert policy.isolated_channel_ids == (str(CHANNEL_ID),)

    ended = _interaction()
    await _button(screen, END_LABEL).callback(ended)
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant.id)
    assert policy.isolated_channel_ids == (), "ending isolation clears the channel"
