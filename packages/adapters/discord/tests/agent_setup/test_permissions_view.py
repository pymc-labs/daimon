"""The Permissions screen: what it offers, who may click, and what a click stores."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.agent_setup.permissions_view import (
    COPY_LABEL,
    RELEASE_LABEL,
    PermissionsView,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.access_policy import OPEN_RULE, AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.channel_rules import ChannelRuleStatus
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.security_audit import list_events
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


OWN = ChannelRule(readers="own", writers="own")


def _view(
    runtime: DiscordRuntime,
    account_id: uuid.UUID,
    *,
    rule: ChannelRule = OPEN_RULE,
    agents: tuple[str, ...] = (),
) -> PermissionsView:
    state = PanelState(
        roster=[],
        selected=None,
        account_id=account_id,
        is_admin=True,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        channel_name="Team Alpha",
    )
    status = ChannelRuleStatus(rule, agents)
    return PermissionsView(state, runtime=runtime, allowed_user_id=42, status=status)


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


def _selects(view: discord.ui.LayoutView) -> list[Any]:
    return [n for n in _walk(view) if isinstance(n, discord.ui.Select)]


async def _choose(view: discord.ui.LayoutView, index: int, value: str, click: MagicMock) -> None:
    select = _selects(view)[index]
    select._values = [value]  # pyright: ignore[reportPrivateUsage]  # a real dispatch sets this
    await select.callback(click)


def test_screen_offers_both_sides_a_copy_and_a_release(account_id: uuid.UUID) -> None:
    runtime = _runtime(MagicMock())
    open_view = _view(runtime, account_id)
    assert _labels(open_view) == {"◀ Back", COPY_LABEL, "Done"}, "an open channel can take a copy"
    defaults = [next(o.value for o in s.options if o.default) for s in _selects(open_view)]
    assert defaults == ["any", "any"], "each select shows the current side"
    kept = _view(runtime, account_id, rule=OWN, agents=("alpha",))
    assert _labels(kept) == {"◀ Back", "Done"}, "own agents are released by changing readers"
    assert (
        "Who can read it: Only its own agents · Who can post: Only its own agents · "
        "Agents kept here: alpha"
    ) in _text(kept)
    inside = _view(runtime, account_id, rule=ChannelRule(readers="inside"), agents=("alpha",))
    assert RELEASE_LABEL in _labels(inside), "agents still kept here can be released"


async def test_a_member_who_lost_manage_server_changes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
        account = await make_account(session, tenant=tenant)
    view = _view(_runtime(db_session_factory), account.id)
    member = _interaction(admin=False)
    await _choose(view, 1, "none", member)
    member.response.send_message.assert_awaited_once()
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant.id)
        (event,) = await list_events(session, tenant_id=tenant.id)
    assert policy == TenantAccessPolicy(), "nothing was changed"
    assert (event.tool_name, event.outcome, event.reason) == (
        "panel:channel_rule",
        "denied",
        "needs_admin",
    ), "the refusal is audited"


async def test_own_readers_refuse_a_shared_agent_then_copy_it_and_release(
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
    await _choose(_view(runtime, account.id), 0, "own", refused)
    screen = refused.response.edit_message.call_args.kwargs["view"]
    assert "also answers outside this channel" in _text(screen), "the refusal says why"
    assert "Nothing was changed" in _text(screen)

    copied = _interaction()
    await _button(screen, COPY_LABEL).callback(copied)
    screen = copied.response.edit_message.call_args.kwargs["view"]
    assert "team-alpha, a copy of shared, is its own agent" in _text(screen)
    async with db_session_factory() as session:
        scope = await get_scope(
            session, scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=str(CHANNEL_ID))
        )
        policy = await load_access_policy(session, tenant_id=tenant.id)
    assert scope is not None and scope.agent_name == "team-alpha", "the copy answers here"
    assert policy.channel_rules == {str(CHANNEL_ID): OWN}

    inside = _interaction()
    await _choose(screen, 0, "inside", inside)
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant.id)
    assert policy.channel_rules == {str(CHANNEL_ID): ChannelRule(readers="inside")}
    assert policy.agent_rules == {"team-alpha": AgentRule(runs_in=(str(CHANNEL_ID),))}, (
        "the copy keeps its rule"
    )

    screen = inside.response.edit_message.call_args.kwargs["view"]
    released = _interaction()
    await _button(screen, RELEASE_LABEL).callback(released)
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant.id)
    assert policy.agent_rules == {}, "releasing drops the copy's rule"
    assert "may now run elsewhere" in _text(released.response.edit_message.call_args.kwargs["view"])


async def test_writers_none_closes_the_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
        account = await make_account(session, tenant=tenant)
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(
                channel_rules={str(CHANNEL_ID): ChannelRule(readers="inside")}
            ),
        )
    click = _interaction()
    rule = ChannelRule(readers="inside")
    await _choose(_view(_runtime(db_session_factory), account.id, rule=rule), 1, "none", click)
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant.id)
    assert policy.channel_rules == {str(CHANNEL_ID): ChannelRule(readers="inside", writers="none")}
