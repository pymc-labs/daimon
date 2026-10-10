"""This channel's environment on Who answers where: what it shows, who may pick, what a pick stores."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
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
    MAX_SETUP_CONVERSATION_LINKS,
    RoutingLine,
    RoutingView,
    build_environments_block,
    build_routing_view,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.answering_map import AnsweringMap, ChannelEnvironment, SetupThreadRef
from daimon.core.channel_environments import (
    ENVIRONMENT_OPTION_INHERIT,
    NOT_OFFERED_NOTE,
    EnvironmentPicker,
    save_scope_environment,
)
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.roster import RosterAgent
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.scoped_config_read import get_scope
from daimon.testing import ma_environment
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import (
    MARouter,
    build_fake_anthropic,
    build_no_retry_anthropic,
    list_response,
)
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
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([]))

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
    account_id: uuid.UUID,
    *,
    is_admin: bool,
    answering_map: AnsweringMap | None = None,
    guild_id: int = GUILD_ID,
    channel_name: str = "growth",
    roster_agents: tuple[RosterAgent, ...] = (),
) -> PanelState:
    return PanelState(
        roster=[],
        selected=None,
        account_id=account_id,
        is_admin=is_admin,
        guild_id=guild_id,
        channel_id=CHANNEL_ID,
        channel_name=channel_name,
        deployment_default=DEFAULT,
        answering_map=answering_map,
        roster_agents=roster_agents,
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


async def _grant_channel(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, *, channel_id: int = CHANNEL_ID
) -> None:
    async with factory() as session, session.begin():
        await set_channel_admins(
            session,
            tenant_id=tenant_id,
            platform="discord",
            channel_id=str(channel_id),
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

    assert text is not None, "the block fits the whole card's budget"
    assert "<#0> → **env-0**" in text, "a channel line reads channel → environment"
    assert f"<#{MAX_ENVIRONMENT_LINES}>" not in text and "-# and 2 more" in text, (
        "the list is bounded and counts what it cut"
    )
    assert text.index("Server default → **shared**") < text.index("Deployment default"), (
        "the server default comes before the deployment fall-through"
    )
    assert "-# Not used while a server default is set." in text, (
        "a server default takes the deployment default out of the cascade"
    )


def test_the_block_with_nothing_set_names_only_the_deployment_default() -> None:
    text = build_environments_block(AnsweringMap(deployment_environment="default"))

    assert text is not None, "the block fits the whole card's budget"
    assert "-# no channel picks its own environment yet" in text, "no channel rows"
    assert "-# no server default" in text, "no tenant row"
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
    assert select.placeholder == "Environment for #growth", "the select names its channel"


def test_the_block_cuts_channel_lines_to_the_room_left_and_keeps_the_defaults() -> None:
    answering_map = AnsweringMap(
        channel_environments=tuple(
            ChannelEnvironment(channel_id=str(CHANNEL_ID + i), environment_name="e" * 40)
            for i in range(MAX_ENVIRONMENT_LINES)
        ),
        tenant_environment="shared",
        deployment_environment="default",
    )

    text = build_environments_block(answering_map, max_chars=300)
    squeezed = build_environments_block(answering_map, max_chars=20)

    assert text is not None and len(text) <= 300, "the block stays inside the room it is given"
    assert f"<#{CHANNEL_ID}>" in text and "more" in text, (
        "it keeps the first lines and counts the rest"
    )
    assert "Server default → **shared**" in text, "the defaults always show"
    assert squeezed is None, "a block whose defaults do not fit is left off, not cut mid-line"


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
    interaction = _interaction(admin=True)

    picker = await load_environment_picker(interaction, runtime=runtime, state=state)
    view = await build_routing_view(
        interaction, runtime=runtime, state=state, allowed_user_id=USER_ID
    )

    assert picker is None, "a failed listing offers no picker"
    assert _select(view) is None, "the screen draws without the select"
    assert "**Environments**" in _text(view), "and still shows every channel's environment"


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
    assert isinstance(rerendered, RoutingView), "the pick redraws the routing screen"
    assert f"<#{CHANNEL_ID}> → **science**" in _text(rerendered), "the screen shows the pick"
    assert "now runs in the science environment" in interaction.followup.send.call_args.args[0], (
        "the reader is told what changed"
    )

    cleared = _select(rerendered)
    assert cleared is not None, "the redrawn screen keeps the select"
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
    assert select is not None, "the rendered picker offers the select"
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
    assert select is not None, "the rendered picker offers the select"
    select._values = ["env:gone"]  # pyright: ignore[reportPrivateUsage]  # a real dispatch sets this
    interaction = _interaction(admin=True)

    await select.callback(interaction)

    assert "no longer exists" in interaction.followup.send.call_args.args[0], (
        "the reader is told the environment is gone"
    )
    assert (
        await get_scope(
            db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=str(CHANNEL_ID))
        )
        is None
    ), "nothing is written for a deleted environment"


async def test_a_forged_value_is_answered_and_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant_id, account_id = await _seed(db_session_factory)
    runtime = _runtime(db_session_factory, _anthropic(tenant_id, "science"))
    view = RoutingView(
        _state(account_id, is_admin=True, answering_map=AnsweringMap()),
        runtime=runtime,
        allowed_user_id=USER_ID,
        environment_picker=EnvironmentPicker(
            channel_id=str(CHANNEL_ID), own=None, inherited=None, names=("science",)
        ),
    )
    select = _select(view)
    assert select is not None, "the rendered picker offers the select"
    select._values = ["science"]  # pyright: ignore[reportPrivateUsage]  # a value no option carries
    interaction = _interaction(admin=True)

    await select.callback(interaction)

    assert interaction.followup.send.call_args.args[0] == NOT_OFFERED_NOTE, (
        "the deferred click gets an answer instead of hanging"
    )
    assert (
        await get_scope(
            db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=str(CHANNEL_ID))
        )
        is None
    ), "a forged value writes nothing"


async def test_an_admin_of_another_channel_is_refused_by_the_select(
    db_session_factory: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant_id, account_id = await _seed(db_session_factory)
    await _grant_channel(db_session_factory, tenant_id, channel_id=CHANNEL_ID + 1)
    runtime = _runtime(db_session_factory, _anthropic(tenant_id, "science"))
    state = _state(account_id, is_admin=False, answering_map=AnsweringMap())
    view = RoutingView(
        state,
        runtime=runtime,
        allowed_user_id=USER_ID,
        environment_picker=EnvironmentPicker(
            channel_id=str(CHANNEL_ID), own=None, inherited=None, names=("science",)
        ),
    )
    select = _select(view)
    assert select is not None, "the rendered picker offers the select"
    select._values = ["env:science"]  # pyright: ignore[reportPrivateUsage]  # a real dispatch sets this
    interaction = _interaction(admin=False)

    picker = await load_environment_picker(interaction, runtime=runtime, state=state)
    await select.callback(interaction)

    assert picker is None, "another channel's grant draws no picker here"
    interaction.response.send_message.assert_awaited_once_with(REFUSED_MESSAGE, ephemeral=True)
    assert (
        await get_scope(
            db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=str(CHANNEL_ID))
        )
        is None
    ), "a grant on another channel writes nothing here"


async def test_a_channel_admin_of_a_sealed_channel_is_refused_an_open_network(
    db_session_factory: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    """In a sealed channel the select keeps unrestricted networking for server admins."""
    tenant_id, account_id = await _seed(db_session_factory)
    await _grant_channel(db_session_factory, tenant_id)
    async with db_session_factory() as session, session.begin():
        await set_access_policy(
            session,
            tenant_id=tenant_id,
            policy=TenantAccessPolicy(sealed_channel_ids=(str(CHANNEL_ID),)),
        )
    runtime = _runtime(db_session_factory, _anthropic(tenant_id, "science"))
    state = _state(account_id, is_admin=False, answering_map=AnsweringMap())
    interaction = _interaction(admin=False)
    view = await build_routing_view(
        interaction, runtime=runtime, state=state, allowed_user_id=USER_ID
    )
    select = _select(view)
    assert select is not None, "the channel admin still gets the select"

    select._values = ["env:science"]  # pyright: ignore[reportPrivateUsage]  # a real dispatch sets this
    await select.callback(interaction)

    assert "Only turns inside" in interaction.followup.send.call_args.args[0], (
        "the refusal names the rule"
    )
    assert (
        await get_scope(
            db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=str(CHANNEL_ID))
        )
        is None
    ), "the refused pick writes nothing"


async def test_a_server_admin_is_sent_to_chat_to_confirm_an_open_network_in_a_sealed_channel(
    db_session_factory: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    """The select has no confirm step, so it writes nothing and points at chat, which asks."""
    tenant_id, account_id = await _seed(db_session_factory)
    async with db_session_factory() as session, session.begin():
        await set_access_policy(
            session,
            tenant_id=tenant_id,
            policy=TenantAccessPolicy(sealed_channel_ids=(str(CHANNEL_ID),)),
        )
    runtime = _runtime(db_session_factory, _anthropic(tenant_id, "science"))
    state = _state(account_id, is_admin=True, answering_map=AnsweringMap())
    interaction = _interaction(admin=True)
    view = await build_routing_view(
        interaction, runtime=runtime, state=state, allowed_user_id=USER_ID
    )
    select = _select(view)
    assert select is not None, "a server admin gets the select"

    select._values = ["env:science"]  # pyright: ignore[reportPrivateUsage]  # a real dispatch sets this
    await select.callback(interaction)

    assert interaction.followup.send.call_args.args[0].endswith(
        "\n\nAsk me in chat to make this change for #growth, then confirm when I ask."
    )
    assert (
        await get_scope(
            db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=str(CHANNEL_ID))
        )
        is None
    ), "the unconfirmed pick writes nothing"


async def test_a_channel_admin_never_sees_another_isolated_channels_own_environment(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Its name could name that channel's client; a server admin still sees every name."""
    tenant_id, account_id = await _seed(db_session_factory)
    await _grant_channel(db_session_factory, tenant_id)
    isolated = str(CHANNEL_ID + 1)
    async with db_session_factory() as session, session.begin():
        await set_access_policy(
            session,
            tenant_id=tenant_id,
            policy=TenantAccessPolicy(
                sealed_channel_ids=(isolated,), isolated_channel_ids=(isolated,)
            ),
        )
        await save_scope_environment(
            session,
            tenant_id=tenant_id,
            channel_id=isolated,
            environment_name="acme",
            actor_account_id=None,
        )
    runtime = _runtime(db_session_factory, _anthropic(tenant_id, "acme", "science"))

    async def names(*, admin: bool) -> tuple[str, ...]:
        picker = await load_environment_picker(
            _interaction(admin=admin),
            runtime=runtime,
            state=_state(account_id, is_admin=admin, answering_map=AnsweringMap()),
        )
        assert picker is not None, "both get the picker"
        return picker.names

    assert await names(admin=False) == ("science",), "the isolated channel's own name is hidden"
    assert await names(admin=True) == ("acme", "science"), "a server admin sees every name"


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_the_widest_routing_screen_with_a_picker_fits_discords_budget() -> None:
    snowflake = 900000000000000000
    answering_map = AnsweringMap(
        channel_environments=tuple(
            ChannelEnvironment(channel_id=str(snowflake + i), environment_name="e" * 60)
            for i in range(MAX_ENVIRONMENT_LINES + 5)
        ),
        tenant_environment="e" * 60,
        deployment_environment="e" * 60,
        setup_threads=tuple(
            SetupThreadRef(
                thread_id=str(snowflake + 100 + i),
                parent_channel_id=str(snowflake),
                target_name="t" * 100,
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            for i in range(MAX_SETUP_CONVERSATION_LINKS + 1)
        ),
    )
    lines = [
        RoutingLine(
            channel_label=f"#{'c' * 30}-{i}",
            agent_name="a" * 30,
            audit_line=f"set by <@{snowflake}> on <t:1700000000:d>",
        )
        for i in range(ROUTING_PAGE_SIZE + 1)
    ]
    unrouted = RosterAgent(
        name="u" * 40, ma_agent_id="agent_u", model_id="claude-sonnet-4-5", is_built_in=False
    )
    view = RoutingView(
        _state(
            uuid.uuid4(),
            is_admin=True,
            answering_map=answering_map,
            guild_id=snowflake,
            channel_name="c" * 100,
            roster_agents=(unrouted,),
        ),
        runtime=_runtime(MagicMock(), build_fake_anthropic(lambda _r: httpx.Response(200))),
        allowed_user_id=USER_ID,
        lines=lines,
        server_default=lines[0],
        environment_picker=EnvironmentPicker(
            channel_id=str(CHANNEL_ID),
            own=None,
            inherited="e" * 60,
            names=tuple(f"{'n' * 90}{i}" for i in range(MAX_ENVIRONMENT_OPTIONS)),
        ),
    )

    assert view.total_children_count <= LAYOUT_COMPONENT_BUDGET, "within the component cap"
    assert view.content_length() <= LAYOUT_TEXT_BUDGET, "within the 4000-character cap"
    assert "**Environments**" in _text(view), "the environments block is cut, not dropped"
    select = _select(view)
    assert select is not None and len(select.options) == MAX_ENVIRONMENT_OPTIONS + 1 <= 25, (
        "the select holds the default plus every offered environment"
    )
