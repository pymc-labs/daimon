"""This channel's environment on Who answers where: what it shows, who may pick, what a pick stores."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
from anthropic import AsyncAnthropic
from daimon.adapters.discord.agent_setup.budget import (
    LAYOUT_COMPONENT_BUDGET,
    LAYOUT_TEXT_BUDGET,
    ROUTING_PAGE_SIZE,
)
from daimon.adapters.discord.agent_setup.channel_environment import (
    MAX_ENVIRONMENT_OPTIONS,
    REFUSED_MESSAGE,
    build_environment_select,
    load_environment_picker,
)
from daimon.adapters.discord.agent_setup.routing_view import (
    MAX_ENVIRONMENT_LINES,
    RoutingLine,
    RoutingView,
    build_environments_block,
    build_routing_view,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.answering_map import AnsweringMap, ChannelEnvironment
from daimon.core.channel_environments import ENVIRONMENT_OPTION_INHERIT, EnvironmentPicker
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.scoped_config_read import get_scope
from daimon.testing import ma_environment
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, build_no_retry_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD_ID = 2001
CHANNEL_ID = 900000000000000001
USER_ID = 42
DEFAULT = DeploymentDefault(agent_name="daimon", environment_name="default")


async def _seed(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    async with factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
        account = await make_account(session, tenant=tenant)
    return tenant.id, account.id


def _anthropic(tenant_id: uuid.UUID, *names: str, calls: list[str] | None = None) -> AsyncAnthropic:
    router = MARouter()
    router.add_environment_list(
        *(ma_environment(id=f"env_{name}", name=name, tenant_id=tenant_id) for name in names)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        return router.dispatch(request)

    return build_fake_anthropic(handler)


def _runtime(sessionmaker: Any, anthropic: AsyncAnthropic) -> DiscordRuntime:
    return DiscordRuntime(
        settings=MagicMock(),
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DEFAULT,
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )


def _state(
    account_id: uuid.UUID, *, is_admin: bool, answering_map: AnsweringMap | None = None
) -> PanelState:
    return PanelState(
        roster=[],
        selected=None,
        account_id=account_id,
        is_admin=is_admin,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        channel_name="growth",
        deployment_default=DEFAULT,
        answering_map=answering_map,
    )


def _interaction(*, admin: bool) -> MagicMock:
    interaction = MagicMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = USER_ID
    interaction.user.roles = []
    interaction.user.guild_permissions.administrator = admin
    interaction.user.guild_permissions.manage_guild = False
    interaction.user.guild.owner_id = 999
    interaction.guild = None
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock(
        side_effect=lambda: interaction.response.is_done.configure_mock(return_value=True)
    )
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _walk(item: Any) -> list[Any]:
    found = [item]
    for child in getattr(item, "children", []) or []:
        found.extend(_walk(child))
    return found


def _text(view: Any) -> str:
    return "\n".join(
        str(node.content) for node in _walk(view) if isinstance(node, discord.ui.TextDisplay)
    )


def _select(view: Any) -> discord.ui.Select[Any] | None:
    selects: list[discord.ui.Select[Any]] = [
        node for node in _walk(view) if isinstance(node, discord.ui.Select)
    ]
    return selects[0] if selects else None


async def _grant_channel(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
    async with factory() as session, session.begin():
        await set_channel_admins(
            session,
            tenant_id=tenant_id,
            platform="discord",
            channel_id=str(CHANNEL_ID),
            role_ids=[],
            user_ids=[str(USER_ID)],
            actor_account_id=None,
        )


# ---------------------------------------------------------------------------
# What the screen shows
# ---------------------------------------------------------------------------


def test_the_block_lists_channels_then_the_defaults() -> None:
    rows = tuple(
        ChannelEnvironment(channel_id=f"{index}", environment_name=f"env-{index}")
        for index in range(MAX_ENVIRONMENT_LINES + 2)
    )
    text = build_environments_block(
        AnsweringMap(
            channel_environments=rows, tenant_environment="shared", deployment_environment="default"
        )
    )

    assert "<#0> → **env-0**" in text, "a channel line reads channel → environment"
    assert f"<#{MAX_ENVIRONMENT_LINES}>" not in text and "-# and 2 more" in text, (
        "the list is bounded and counts what it cut"
    )
    assert text.index("Server default → **shared**") < text.index("Deployment default"), (
        "the server default comes before the deployment fall-through"
    )
    assert "-# not in effect while a server default is set" in text


def test_the_block_with_nothing_set_names_only_the_deployment_default() -> None:
    text = build_environments_block(AnsweringMap(deployment_environment="default"))

    assert "-# no channel picks its own environment yet" in text
    assert "-# no server default" in text
    assert "Deployment default → **default**" in text and "not in effect" not in text, (
        "with no rows at all, every channel runs where it did before"
    )


def test_the_select_leads_with_the_default_and_marks_the_current_pick() -> None:
    select = build_environment_select(
        EnvironmentPicker(
            channel_id=str(CHANNEL_ID), own="science", inherited="shared", names=("gpu", "science")
        ),
        channel_name="growth",
    )

    first = select.options[0]
    assert (first.value, first.label, first.default) == (
        ENVIRONMENT_OPTION_INHERIT,
        "Use the default (shared)",
        False,
    ), "the first option hands the channel back to the default and names it"
    assert [(o.value, o.default) for o in select.options[1:]] == [
        ("env:gpu", False),
        ("env:science", True),
    ], "the channel's own pick is pre-selected"
    assert select.placeholder == "Environment for #growth"


# ---------------------------------------------------------------------------
# Who gets the picker
# ---------------------------------------------------------------------------


async def test_members_get_no_picker_and_no_environment_listing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed(db_session_factory)
    calls: list[str] = []
    runtime = _runtime(db_session_factory, _anthropic(tenant_id, "science", calls=calls))
    state = _state(account_id, is_admin=False, answering_map=AnsweringMap())

    picker = await load_environment_picker(_interaction(admin=False), runtime=runtime, state=state)

    assert picker is None, "a member with no grant for this channel cannot pick"
    assert calls == [], "a member costs one grant read and no environment listing"


async def test_server_and_channel_admins_get_the_picker(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed(db_session_factory)
    runtime = _runtime(db_session_factory, _anthropic(tenant_id, "science", "Beta", "alpha"))
    answering_map = AnsweringMap(tenant_environment="shared", deployment_environment="default")

    admin = await load_environment_picker(
        _interaction(admin=True),
        runtime=runtime,
        state=_state(account_id, is_admin=True, answering_map=answering_map),
    )
    await _grant_channel(db_session_factory, tenant_id)
    channel_admin = await load_environment_picker(
        _interaction(admin=False),
        runtime=runtime,
        state=_state(account_id, is_admin=False, answering_map=answering_map),
    )

    assert admin is not None and admin.names == ("alpha", "Beta", "science"), (
        "a server admin picks from the server's environments, sorted case-insensitively"
    )
    assert admin.inherited == "shared", "the default names what the channel falls to"
    assert channel_admin == admin, "an admin of this channel gets the same picker"


async def test_a_failed_listing_hides_only_the_picker(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, account_id = await _seed(db_session_factory)
    runtime = _runtime(
        db_session_factory, build_no_retry_anthropic(lambda _r: httpx.Response(500, json={}))
    )
    state = _state(account_id, is_admin=True, answering_map=AnsweringMap())

    assert await load_environment_picker(
        _interaction(admin=True), runtime=runtime, state=state
    ) is (None)


# ---------------------------------------------------------------------------
# Picking
# ---------------------------------------------------------------------------


async def test_picking_saves_this_channel_and_clearing_removes_the_row(
    db_session_factory: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant_id, account_id = await _seed(db_session_factory)
    runtime = _runtime(db_session_factory, _anthropic(tenant_id, "science"))
    state = _state(account_id, is_admin=True)
    interaction = _interaction(admin=True)
    view = await build_routing_view(
        interaction, runtime=runtime, state=state, allowed_user_id=USER_ID
    )
    select = _select(view)
    assert select is not None, "a server admin gets the environment select"

    select._values = ["env:science"]  # pyright: ignore[reportPrivateUsage]  # a real dispatch sets this
    await select.callback(interaction)

    scope = ChannelScopeRef(tenant_id=tenant_id, channel_id=str(CHANNEL_ID))
    row = await get_scope(db_session, scope=scope)
    assert row is not None and row.environment_name == "science", "the pick is stored"
    assert row.agent_name is None, "picking an environment leaves the channel's agent alone"
    rerendered = interaction.edit_original_response.call_args.kwargs["view"]
    assert isinstance(rerendered, RoutingView)
    assert f"<#{CHANNEL_ID}> → **science**" in _text(rerendered), "the screen shows the pick"
    assert "now runs in the science environment" in interaction.followup.send.call_args.args[0]

    cleared = _select(rerendered)
    assert cleared is not None
    cleared._values = [ENVIRONMENT_OPTION_INHERIT]  # pyright: ignore[reportPrivateUsage]  # a real dispatch sets this
    await cleared.callback(_interaction(admin=True))

    assert await get_scope(db_session, scope=scope) is None, (
        "handing the channel back leaves no row, exactly as before any pick"
    )


async def test_a_pick_is_refused_when_the_caller_is_no_longer_allowed(
    db_session_factory: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant_id, account_id = await _seed(db_session_factory)
    runtime = _runtime(db_session_factory, _anthropic(tenant_id, "science"))
    state = _state(account_id, is_admin=True, answering_map=AnsweringMap())
    view = RoutingView(
        state,
        runtime=runtime,
        allowed_user_id=USER_ID,
        environment_picker=EnvironmentPicker(
            channel_id=str(CHANNEL_ID), own=None, inherited=None, names=("science",)
        ),
    )
    select = _select(view)
    assert select is not None
    select._values = ["env:science"]  # pyright: ignore[reportPrivateUsage]  # a real dispatch sets this
    interaction = _interaction(admin=False)

    await select.callback(interaction)

    interaction.response.send_message.assert_awaited_once_with(REFUSED_MESSAGE, ephemeral=True)
    assert (
        await get_scope(
            db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=str(CHANNEL_ID))
        )
        is None
    ), "the rendered picker is a hint; the live check refuses the write"


async def test_an_environment_gone_since_render_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant_id, account_id = await _seed(db_session_factory)
    runtime = _runtime(db_session_factory, _anthropic(tenant_id, "science"))
    state = _state(account_id, is_admin=True, answering_map=AnsweringMap())
    view = RoutingView(
        state,
        runtime=runtime,
        allowed_user_id=USER_ID,
        environment_picker=EnvironmentPicker(
            channel_id=str(CHANNEL_ID), own=None, inherited=None, names=("gone",)
        ),
    )
    select = _select(view)
    assert select is not None
    select._values = ["env:gone"]  # pyright: ignore[reportPrivateUsage]  # a real dispatch sets this
    interaction = _interaction(admin=True)

    await select.callback(interaction)

    assert "no longer exists" in interaction.followup.send.call_args.args[0]
    assert (
        await get_scope(
            db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=str(CHANNEL_ID))
        )
        is None
    )


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_the_widest_routing_screen_with_a_picker_fits_discords_budget() -> None:
    answering_map = AnsweringMap(
        channel_environments=tuple(
            ChannelEnvironment(channel_id=str(CHANNEL_ID + i), environment_name="e" * 40)
            for i in range(MAX_ENVIRONMENT_LINES + 5)
        ),
        tenant_environment="e" * 40,
        deployment_environment="e" * 40,
    )
    lines = [
        RoutingLine(
            channel_label=f"#{'c' * 30}-{i}",
            agent_name="a" * 30,
            audit_line="set by <@900000000000000001> on <t:1700000000:d>",
        )
        for i in range(ROUTING_PAGE_SIZE + 1)
    ]
    view = RoutingView(
        _state(uuid.uuid4(), is_admin=True, answering_map=answering_map),
        runtime=_runtime(MagicMock(), build_fake_anthropic(lambda _r: httpx.Response(200))),
        allowed_user_id=USER_ID,
        lines=lines,
        server_default=lines[0],
        environment_picker=EnvironmentPicker(
            channel_id=str(CHANNEL_ID),
            own=None,
            inherited="e" * 40,
            names=tuple(f"{'n' * 90}{i}" for i in range(MAX_ENVIRONMENT_OPTIONS)),
        ),
    )

    assert view.total_children_count <= LAYOUT_COMPONENT_BUDGET
    assert view.content_length() <= LAYOUT_TEXT_BUDGET
    select = _select(view)
    assert select is not None and len(select.options) == MAX_ENVIRONMENT_OPTIONS + 1 <= 25
