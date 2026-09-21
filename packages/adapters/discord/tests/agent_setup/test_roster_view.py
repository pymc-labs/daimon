"""The roster screen: what it says, what it costs, and what its buttons do."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from daimon.adapters.discord.agent_setup.budget import (
    LAYOUT_COMPONENT_BUDGET,
    ROSTER_CHROME_COST,
    ROSTER_PAGE_SIZE,
    ROSTER_ROW_COST,
)
from daimon.adapters.discord.agent_setup.navigation import INVOKER_ONLY_MESSAGE
from daimon.adapters.discord.agent_setup.roster_view import RosterView, roster_rows
from daimon.adapters.discord.agent_setup.state import PanelState, ThreadContext
from daimon.adapters.discord.runtime import DiscordRuntime, build_turn_deps
from daimon.core.config import Settings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.roster import RosterAgent, order_roster
from daimon.core.scope import (
    ChannelConfigRow,
    ChannelScopeRef,
    DeploymentDefault,
    TenantConfigRow,
)
from daimon.core.setup_conversations import EMPTY_ROSTER_COPY, setup_target_label
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_agent_bindings import get_binding
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD_ID = 111
CHANNEL_ID = 222


def _agent(name: str, *, tier: str | None = None, built_in: bool = False) -> RosterAgent:
    return RosterAgent(
        name=name,
        ma_agent_id=f"ag_{name}",
        model_id="claude-sonnet-4-6",
        is_built_in=built_in,
        answering_tier=tier,  # pyright: ignore[reportArgumentType]  # ConfigTier literal, spelled by the caller
    )


def _state(
    *,
    agents: tuple[RosterAgent, ...],
    answering: RosterAgent | None = None,
    is_admin: bool = False,
    attributions: dict[str, str] | None = None,
    thread_context: ThreadContext | None = None,
    channel_rows: list[ChannelConfigRow] | None = None,
    tenant_row: TenantConfigRow | None = None,
    deployment_default: DeploymentDefault | None = None,
    account_id: uuid.UUID | None = None,
) -> PanelState:
    return PanelState(
        roster=[],
        selected=None,
        account_id=account_id or uuid.uuid4(),
        is_admin=is_admin,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        channel_name="general",
        cascade_view=(tenant_row, channel_rows if channel_rows is not None else []),
        deployment_default=deployment_default or DeploymentDefault(),
        roster_agents=agents,
        answering=answering,
        selected_agent=answering,
        attributions=attributions or {},
        thread_context=thread_context,
    )


def _runtime(
    *, sessionmaker: async_sessionmaker[AsyncSession], anthropic: Any, default: DeploymentDefault
) -> DiscordRuntime:
    settings = Settings.model_validate(
        {
            "database": {"url": "postgresql+asyncpg://test:test@localhost/daimon_test"},
            "anthropic": {"api_key": "test"},
        }
    )
    cache = new_resolver_cache()
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=default,
        resolver_cache=cache,
        turn_deps=build_turn_deps(
            settings,
            anthropic,
            sessionmaker,
            deployment_default=default,
            resolver_cache=cache,
            billing_config=None,
        ),
    )


def _clicker(*, user_id: int = 42, responded: bool = False) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = user_id
    interaction.guild_id = GUILD_ID
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=responded)
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _structure(view: discord.ui.LayoutView) -> list[tuple[str, object, object]]:
    """Component types, labels and text — everything a reader can tell apart."""
    return [
        (
            type(child).__name__,
            getattr(child, "label", None),
            getattr(child, "content", None),
        )
        for child in view.walk_children()
    ]


def _labels(view: discord.ui.LayoutView) -> list[str]:
    return [
        child.label or "" for child in view.walk_children() if isinstance(child, discord.ui.Button)
    ]


def _text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        child.content for child in view.walk_children() if isinstance(child, discord.ui.TextDisplay)
    )


# ---------------------------------------------------------------------------
# Pure: rows, ordering, attribution
# ---------------------------------------------------------------------------


def test_roster_rows_put_the_answering_agent_first_then_name_order() -> None:
    answering = _agent("zeta", tier="channel")
    others = [_agent("Beta"), _agent("alpha")]
    ordered = order_roster([answering, *others], answering_here="zeta")
    rows = roster_rows(_state(agents=ordered, answering=answering), attributions={})

    assert [row.agent.name for row in rows] == ["zeta", "alpha", "Beta"], (
        "the agent answering here comes first; the rest follow casefolded name order"
    )
    assert rows[0].status == "answers_here", "the first row is the one the screen opened about"
    assert [row.status for row in rows[1:]] == ["unrouted", "unrouted"], (
        "an agent nothing routes to is reported as unrouted, not as available"
    )


def test_roster_rows_attribute_only_the_agents_whose_creator_resolved() -> None:
    made_by_someone = _agent("churn-explorer")
    made_by_the_guild = _agent("daimon", built_in=True)
    rows = roster_rows(
        _state(agents=(made_by_someone, made_by_the_guild)),
        attributions={made_by_someone.ma_agent_id: "<@42>"},
    )

    by_name = {row.agent.name: row for row in rows}
    assert by_name["churn-explorer"].attribution == "<@42>", (
        "a creator that resolved to a Discord principal is named"
    )
    assert by_name["daimon"].attribution is None, (
        "an agent with no resolved creator gets no invented attribution"
    )


def test_roster_rows_report_a_server_default_and_a_channel_elsewhere() -> None:
    tenant_id = uuid.uuid4()
    default_agent = _agent("workspace-bot")
    elsewhere = _agent("growth-bot")
    rows = roster_rows(
        _state(
            agents=(default_agent, elsewhere),
            tenant_row=TenantConfigRow(tenant_id=tenant_id, agent_name="workspace-bot"),
            channel_rows=[
                ChannelConfigRow(tenant_id=tenant_id, channel_id="999", agent_name="growth-bot")
            ],
        ),
        attributions={},
    )

    statuses = {row.agent.name: row.status for row in rows}
    assert statuses["workspace-bot"] == "server_default", (
        "the workspace default must be identified as such"
    )
    assert statuses["growth-bot"] == "routed_elsewhere", (
        "an agent routed in another channel is not unrouted"
    )


def test_a_built_in_agent_that_is_the_server_default_shows_its_routing_status() -> None:
    tenant_id = uuid.uuid4()
    built_in_default = _agent("daimon", built_in=True)
    view = RosterView(
        _state(
            agents=(built_in_default,),
            tenant_row=TenantConfigRow(tenant_id=tenant_id, agent_name="daimon"),
        ),
        runtime=MagicMock(),
        allowed_user_id=42,
    )

    text = _text(view)
    assert "**daimon**\n-# server default" in text, (
        "the widest routing fact is what the reader acts on, so it leads the row"
    )
    assert "built in" not in text, "the compact roster omits provenance metadata"


def test_the_deployment_default_is_named_for_its_own_tier() -> None:
    view = RosterView(
        _state(
            agents=(_agent("specialist"),),
            deployment_default=DeploymentDefault(agent_name="specialist"),
        ),
        runtime=MagicMock(),
        allowed_user_id=42,
    )

    assert "**specialist**\n-# deployment default" in _text(view), (
        "a deployment default is changed somewhere else than a server default, and must say so"
    )


def test_a_built_in_agent_that_is_no_default_reads_as_unrouted() -> None:
    view = RosterView(
        _state(agents=(_agent("daimon", built_in=True),)), runtime=MagicMock(), allowed_user_id=42
    )

    text = _text(view)
    assert "**daimon**\n-# not assigned" in text, (
        "the row reports actual routing rather than built-in provenance"
    )


# ---------------------------------------------------------------------------
# Rendering: empty state, budget, paging, thread line, role parity
# ---------------------------------------------------------------------------


def test_empty_roster_shows_the_setup_copy_and_keeps_the_setup_action() -> None:
    view = RosterView(_state(agents=()), runtime=MagicMock(), allowed_user_id=42)

    assert EMPTY_ROSTER_COPY in _text(view), "an empty roster says what to do next"
    assert not any(isinstance(c, discord.ui.Section) for c in view.walk_children()), (
        "an empty roster renders no agent rows"
    )
    assert setup_target_label(None) in _labels(view), (
        "setup stays available with no agent answering here"
    )


def test_roster_preserves_long_names_but_bounds_the_targeted_setup_label() -> None:
    answering = _agent("a" * 64, tier="channel")
    view = RosterView(
        _state(agents=(answering,), answering=answering),
        runtime=MagicMock(),
        allowed_user_id=42,
    )

    assert f"**{answering.name}**\n-# answers here" in _text(view), (
        "the readable row keeps the full agent identity and its status"
    )
    setup_label = next(label for label in _labels(view) if label.startswith("⚙️ Manage"))
    assert len(setup_label) <= 30 and setup_label.endswith("…"), (
        "the targeted action stays within its product copy limit and marks truncation"
    )


def test_a_full_page_with_a_thread_line_stays_within_the_component_budget() -> None:
    answering = _agent("answering", tier="channel")
    agents = (answering, *(_agent(f"agent-{index:02d}") for index in range(40)))
    state = _state(
        agents=agents,
        answering=answering,
        attributions={agent.ma_agent_id: "<@42>" for agent in agents},
        thread_context=ThreadContext(kind="setup", responder_name="Daimon", target_name="agent-00"),
    )

    view = RosterView(state, runtime=MagicMock(), allowed_user_id=42)

    expected = ROSTER_CHROME_COST + ROSTER_PAGE_SIZE * ROSTER_ROW_COST
    assert view.total_children_count == expected, (
        f"the widest roster page must cost exactly the documented chrome plus rows; "
        f"got {view.total_children_count}, expected {expected}"
    )
    assert view.total_children_count <= LAYOUT_COMPONENT_BUDGET, (
        "discord.py refuses a LayoutView above the component budget"
    )


def test_page_row_is_omitted_when_the_roster_fits_on_one_page() -> None:
    view = RosterView(
        _state(agents=tuple(_agent(f"agent-{i}") for i in range(ROSTER_PAGE_SIZE))),
        runtime=MagicMock(),
        allowed_user_id=42,
    )

    assert "◀ Previous" not in _labels(view), "a single page needs no pager"
    assert "Page 1 of 1" not in _text(view), "a single page needs no page counter"


async def test_next_and_previous_clamp_at_the_ends_of_the_roster() -> None:
    state = _state(agents=tuple(_agent(f"agent-{i:02d}") for i in range(ROSTER_PAGE_SIZE + 1)))
    view = RosterView(state, runtime=MagicMock(), allowed_user_id=42)

    await view._on_previous(_clicker())  # pyright: ignore[reportPrivateUsage]  # callback under test
    assert state.roster_page == 0, "Previous on the first page stays on the first page"

    await view._on_next(_clicker())  # pyright: ignore[reportPrivateUsage]  # callback under test
    assert state.roster_page == 1, "Next advances one page"

    last = RosterView(state, runtime=MagicMock(), allowed_user_id=42)
    await last._on_next(_clicker())  # pyright: ignore[reportPrivateUsage]  # callback under test
    assert state.roster_page == 1, "Next on the last page stays on the last page"


async def test_paging_edits_the_panel_in_place_rather_than_sending_a_message() -> None:
    state = _state(agents=tuple(_agent(f"agent-{i:02d}") for i in range(ROSTER_PAGE_SIZE + 1)))
    view = RosterView(state, runtime=MagicMock(), allowed_user_id=42)
    interaction = _clicker()

    await view._on_next(interaction)  # pyright: ignore[reportPrivateUsage]  # callback under test

    interaction.response.edit_message.assert_awaited_once()
    interaction.followup.send.assert_not_awaited()
    swapped = interaction.response.edit_message.call_args.kwargs["view"]
    assert isinstance(swapped, RosterView), "paging stays on the roster screen"
    assert "Page 2 of 2" in _text(swapped), "the page counter names the page on screen"


def test_a_bound_setup_thread_names_its_responder_and_its_target() -> None:
    answering = _agent("daimon", tier="thread", built_in=True)
    view = RosterView(
        _state(
            agents=(answering, _agent("specialist")),
            answering=answering,
            thread_context=ThreadContext(
                kind="setup", responder_name="Daimon", target_name="specialist"
            ),
        ),
        runtime=MagicMock(),
        allowed_user_id=42,
    )

    text = _text(view)
    assert "In this thread **Daimon** answers while setting up **specialist**" in text, (
        "a setup thread says who answers in it and what is being set up"
    )
    assert "Agents in #general" in text, "the header still names the parent channel"


def test_a_handoff_thread_says_so_without_inventing_a_setup_target() -> None:
    answering = _agent("specialist", tier="thread")
    view = RosterView(
        _state(
            agents=(answering,),
            answering=answering,
            thread_context=ThreadContext(
                kind="handoff", responder_name="specialist", target_name=None
            ),
        ),
        runtime=MagicMock(),
        allowed_user_id=42,
    )

    assert "In this thread **specialist** answers (handed off)" in _text(view), (
        "a handoff thread reports the handoff, not a configuration target"
    )


def test_members_and_admins_get_identical_components() -> None:
    answering = _agent("research-bot", tier="channel")
    agents = (answering, _agent("daimon", built_in=True), _agent("churn-explorer"))

    member = RosterView(
        _state(agents=agents, answering=answering, is_admin=False),
        runtime=MagicMock(),
        allowed_user_id=42,
    )
    admin = RosterView(
        _state(agents=agents, answering=answering, is_admin=True),
        runtime=MagicMock(),
        allowed_user_id=42,
    )

    assert _structure(member) == _structure(admin), (
        "role changes the voice of the routing sentence elsewhere, never this screen's components"
    )


# ---------------------------------------------------------------------------
# Gating and expiry
# ---------------------------------------------------------------------------


async def test_roster_view_refuses_a_caller_who_did_not_run_the_command() -> None:
    view = RosterView(_state(agents=()), runtime=MagicMock(), allowed_user_id=42)
    intruder = _clicker(user_id=7)

    allowed = await view.interaction_check(intruder)

    assert allowed is False, "only the invoker may use the panel"
    assert intruder.response.send_message.call_args.args[0] == INVOKER_ONLY_MESSAGE, (
        "the refusal names the rule"
    )
    assert intruder.response.send_message.call_args.kwargs["ephemeral"] is True, (
        "the refusal is private to the person who clicked"
    )


async def test_roster_view_timeout_replaces_the_panel(mock_interaction: MagicMock) -> None:
    state = _state(agents=(_agent("alice"),))
    view = RosterView(state, runtime=MagicMock(), allowed_user_id=42)
    view.bind_render_interaction(mock_interaction, panel=state)

    await view.on_timeout()

    expired = mock_interaction.edit_original_response.call_args.kwargs["view"]
    assert not any(isinstance(c, discord.ui.Button) for c in expired.walk_children()), (
        "the expired replacement carries no interactive children"
    )
    assert view.timeout == 600, "the panel's timeout is ten minutes"


async def test_a_superseded_roster_view_does_not_rewrite_the_message(
    mock_interaction: MagicMock,
) -> None:
    state = _state(agents=(_agent("alice"),))
    first = RosterView(state, runtime=MagicMock(), allowed_user_id=42)
    first.bind_render_interaction(mock_interaction, panel=state)
    second = RosterView(state, runtime=MagicMock(), allowed_user_id=42)
    second.bind_render_interaction(mock_interaction, panel=state)

    await first.on_timeout()
    mock_interaction.edit_original_response.assert_not_called()

    await second.on_timeout()
    mock_interaction.edit_original_response.assert_called_once()


# ---------------------------------------------------------------------------
# Setup entry and Details, against Postgres
# ---------------------------------------------------------------------------


async def test_setup_targets_the_answering_agent_and_binds_it_without_a_billed_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
        account = await make_account(session, tenant=tenant)
    responder = ma_agent(
        id="agent_daimon", name="daimon", tenant_id=tenant.id, metadata={"daimon_managed": "true"}
    )
    target = ma_agent(id="agent_specialist", name="specialist", tenant_id=tenant.id)

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "opening setup must not create a session or billed turn"
        if request.url.path.endswith("/agents"):
            return list_response(
                [responder.model_dump(mode="json"), target.model_dump(mode="json")]
            )
        assert request.url.path.endswith("/agents/agent_specialist"), (
            "setup validates the exact agent the roster said answers here"
        )
        return httpx.Response(200, json=target.model_dump(mode="json"))

    default = DeploymentDefault(agent_name="specialist")
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(handle),
        default=default,
    )
    answering = RosterAgent(
        name="specialist",
        ma_agent_id="agent_specialist",
        model_id="claude-sonnet-4-6",
        is_built_in=False,
        answering_tier="deployment",
    )
    state = _state(
        agents=(answering,),
        answering=answering,
        deployment_default=default,
        account_id=account.id,
    )
    view = RosterView(state, runtime=runtime, allowed_user_id=42)

    thread = MagicMock(spec=discord.Thread)
    thread.id = 333
    thread.jump_url = "https://discord.com/channels/111/333"
    thread.send = AsyncMock()
    thread.delete = AsyncMock()
    interaction = _clicker()
    interaction.user.mention = "<@42>"
    interaction.guild.owner_id = 999
    interaction.client.user.mention = "<@123>"
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.channel.id = CHANNEL_ID
    interaction.channel.create_thread = AsyncMock(return_value=thread)

    await view._on_setup(interaction)  # pyright: ignore[reportPrivateUsage]  # callback under test

    async with db_session_factory() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id=str(CHANNEL_ID),
            thread_id="333",
        )
    assert binding is not None, "setup routing must be durable"
    assert binding.configuration_target_ma_agent_id == "agent_specialist", (
        "setup targets the agent the roster said answers here, by exact MA identity"
    )
    assert binding.responder_ma_agent_id == "agent_daimon", (
        "Daimon responds regardless of which agent is being set up"
    )


async def test_details_click_loads_the_agent_before_editing_the_panel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
    agent = ma_agent(id="ag_specialist", name="specialist", tenant_id=tenant.id)
    router = MARouter()
    router.add_agent(agent)
    router.add("GET", r"/v1/skills", lambda _r, _m: list_response([]))
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(router.dispatch),
        default=DeploymentDefault(),
    )
    row = RosterAgent(
        name="specialist",
        ma_agent_id="ag_specialist",
        model_id="claude-sonnet-4-6",
        is_built_in=False,
    )
    state = _state(agents=(row,))
    view = RosterView(state, runtime=runtime, allowed_user_id=42)
    interaction = _clicker(responded=True)

    await view._on_details(interaction, agent=row)  # pyright: ignore[reportPrivateUsage]  # callback under test

    assert state.details is not None, "Details is read before the panel is edited"
    assert state.details.name == "specialist", "the loaded details describe the clicked agent"
    assert state.selected_agent == row, "the click moves the panel's selection"
    swapped = interaction.edit_original_response.call_args.kwargs["view"]
    assert type(swapped).__name__ == "DetailsView", "the panel swaps to the Details screen"


async def test_details_click_reports_a_failed_load_and_leaves_the_panel_alone(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.adapters.discord.agent_setup import roster_view as roster_view_module
    from daimon.core.errors import DaimonError

    async def _fail(*_args: object, **_kwargs: object) -> None:
        raise DaimonError("That agent no longer exists. Choose another agent for setup.")

    monkeypatch.setattr(roster_view_module, "load_details_for", _fail)
    row = RosterAgent(
        name="gone", ma_agent_id="ag_gone", model_id="claude-sonnet-4-6", is_built_in=False
    )
    state = _state(agents=(row,))
    view = RosterView(
        state,
        runtime=_runtime(
            sessionmaker=db_session_factory, anthropic=MagicMock(), default=DeploymentDefault()
        ),
        allowed_user_id=42,
    )
    interaction = _clicker(responded=True)

    await view._on_details(interaction, agent=row)  # pyright: ignore[reportPrivateUsage]  # callback under test

    assert state.details is None, "a failed load must not leave a half-built Details on the state"
    interaction.edit_original_response.assert_not_awaited()
    message = interaction.followup.send.call_args.args[0]
    assert "no longer exists" in message, (
        f"the failure is reported through render_error; got {message!r}"
    )


async def test_routing_click_loads_the_answering_map_and_swaps_to_the_routing_screen(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
        account = await make_account(session, tenant=tenant)
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="900"),
            tenant_id=tenant.id,
            agent_name="growth-bot",
            mode="agent",
            actor_account_id=account.id,
        )
    runtime = _runtime(
        sessionmaker=db_session_factory, anthropic=MagicMock(), default=DeploymentDefault()
    )
    state = _state(agents=(_agent("growth-bot"),), account_id=account.id)
    view = RosterView(state, runtime=runtime, allowed_user_id=42)
    interaction = _clicker(responded=True)
    interaction.guild = None

    await view._on_routing(interaction)  # pyright: ignore[reportPrivateUsage]  # callback under test

    assert state.answering_map is not None, "the routing screen is rendered from a freshly read map"
    assert [override.channel_id for override in state.answering_map.channel_overrides] == ["900"], (
        "the map carries the channels that pick their own agent"
    )
    swapped = interaction.edit_original_response.call_args.kwargs["view"]
    assert type(swapped).__name__ == "RoutingView", "the panel swaps to Who answers where"
    text = _text(swapped)
    assert "growth-bot" in text, "the routing screen names the agent each channel routes to"
    assert f"set by <@{42}>" not in text, (
        "attribution comes from the recorded actor, not the caller"
    )
