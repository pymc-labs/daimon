"""Behavioural tests for the Who answers where screen.

The screen exists so a reader can see the cascade instead of inferring it, so
the order of the tiers, the separation of setup conversations from routing
rules, and the honesty of the deployment fall-through are the things under test.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.agent_setup.budget import ROUTING_PAGE_SIZE
from daimon.adapters.discord.agent_setup.routing_view import (
    RoutingLine,
    RoutingView,
    build_routing_container,
    build_routing_sentence,
    load_routing_lines,
    setup_conversation_links,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.answering_map import AnsweringMap, ChannelAnswer, SetupThreadRef, TenantAnswer
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.roster import RosterAgent, paginate
from daimon.core.routing_facts import PRECEDENCE_LINE
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession

_SET_AT = dt.datetime(2026, 2, 1, tzinfo=dt.UTC)


def _agent(name: str) -> RosterAgent:
    return RosterAgent(
        name=name, ma_agent_id=f"ag_{name}", model_id="claude-sonnet-4-6", is_built_in=False
    )


def _map(
    *,
    channels: tuple[ChannelAnswer, ...] = (),
    tenant_default: TenantAnswer | None = None,
    deployment_default: str | None = "daimon",
    setup_threads: tuple[SetupThreadRef, ...] = (),
) -> AnsweringMap:
    return AnsweringMap(
        channel_overrides=channels,
        tenant_default=tenant_default,
        deployment_default=deployment_default,
        tenant_consumes_fallthrough=tenant_default is not None,
        setup_threads=setup_threads,
    )


def _state(
    answering_map: AnsweringMap,
    *,
    account_id: uuid.UUID,
    is_admin: bool = True,
    roster: tuple[RosterAgent, ...] = (),
    routing_page: int = 0,
    roster_page: int = 0,
) -> PanelState:
    return PanelState(
        roster=[],
        selected=None,
        account_id=account_id,
        is_admin=is_admin,
        guild_id=2001,
        channel_id=900,
        channel_name="growth",
        deployment_default=DeploymentDefault(),
        roster_agents=roster,
        answering=roster[0] if roster else None,
        selected_agent=roster[0] if roster else None,
        answering_map=answering_map,
        routing_page=routing_page,
        roster_page=roster_page,
    )


def _make_runtime() -> DiscordRuntime:
    return DiscordRuntime(
        settings=MagicMock(),
        anthropic=build_stub_anthropic(),
        sessionmaker=MagicMock(),
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )


def _interaction(user_id: int = 42) -> MagicMock:
    interaction = MagicMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = user_id
    interaction.guild_id = 2001
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _walk(item: Any) -> list[Any]:
    found = [item]
    for child in getattr(item, "children", []) or []:
        found.extend(_walk(child))
    accessory = getattr(item, "accessory", None)
    if accessory is not None:
        found.extend(_walk(accessory))
    return found


def _text(item: Any) -> str:
    return "\n".join(
        str(node.content) for node in _walk(item) if isinstance(node, discord.ui.TextDisplay)
    )


def _find_button(view: discord.ui.LayoutView, label: str) -> discord.ui.Button[Any]:
    for node in _walk(view):
        if isinstance(node, discord.ui.Button) and node.label == label:
            return node
    raise AssertionError(f"No button labeled {label!r}")


def _lines(count: int) -> list[RoutingLine]:
    return [
        RoutingLine(channel_label=f"#chan-{index}", agent_name=f"bot-{index}", audit_line=None)
        for index in range(count)
    ]


# ---------------------------------------------------------------------------
# Order and tiers
# ---------------------------------------------------------------------------


def test_routing_orders_channels_then_server_default_then_deployment_default() -> None:
    container = build_routing_container(
        paginate(
            [
                RoutingLine(channel_label="#data", agent_name="research-bot", audit_line=None),
                RoutingLine(channel_label="#growth", agent_name="churn-explorer", audit_line=None),
            ],
            page=0,
            page_size=ROUTING_PAGE_SIZE,
        ),
        server_default=RoutingLine(
            channel_label="Server default", agent_name="daimon", audit_line="set by <@77>"
        ),
        deployment_default="fallback-bot",
        deployment_in_effect=False,
        conversations=[],
        sentence=PRECEDENCE_LINE,
    )
    text = _text(container)

    assert "#data → **research-bot**" in text, "a channel override reads channel → agent"
    assert text.index("#data") < text.index("#growth") < text.index("Server default"), (
        "channels come before the server default"
    )
    assert text.index("Server default") < text.index("Deployment default"), (
        "the server default comes before the deployment fall-through"
    )
    assert "-# set by <@77>" in text, "the server default carries its audit line"


def test_deployment_default_is_marked_not_in_effect_when_the_tenant_consumes_the_fallthrough() -> (
    None
):
    """A server default does not sit above the deployment default — it removes it."""
    container = build_routing_container(
        paginate([], page=0, page_size=ROUTING_PAGE_SIZE),
        server_default=RoutingLine(
            channel_label="Server default", agent_name="daimon", audit_line=None
        ),
        deployment_default="fallback-bot",
        deployment_in_effect=False,
        conversations=[],
        sentence=PRECEDENCE_LINE,
    )
    text = _text(container)
    assert "-# not in effect while a server default is set" in text, (
        "naming the deployment default as reachable alongside a server default would be wrong"
    )


def test_deployment_default_is_plain_when_no_server_default_shadows_it() -> None:
    container = build_routing_container(
        paginate([], page=0, page_size=ROUTING_PAGE_SIZE),
        server_default=None,
        deployment_default="fallback-bot",
        deployment_in_effect=True,
        conversations=[],
        sentence=PRECEDENCE_LINE,
    )
    text = _text(container)
    assert "-# no server default" in text, "an install with no server default says so"
    assert "not in effect" not in text, "the fall-through really is in effect here"


# ---------------------------------------------------------------------------
# Setup conversations
# ---------------------------------------------------------------------------


def test_setup_conversations_are_listed_separately_and_bounded() -> None:
    links = [
        f"[Set up bot-{index}](https://discord.com/channels/2001/{index})" for index in range(8)
    ]
    container = build_routing_container(
        paginate(
            [RoutingLine(channel_label="#data", agent_name="research-bot", audit_line=None)],
            page=0,
            page_size=ROUTING_PAGE_SIZE,
        ),
        server_default=None,
        deployment_default=None,
        deployment_in_effect=True,
        conversations=links,
        sentence=PRECEDENCE_LINE,
    )
    text = _text(container)

    assert "**Setup conversations**" in text, "live setup threads get their own heading"
    assert text.index("#data") < text.index("**Setup conversations**"), (
        "a setup conversation is not a routing rule, so it comes after the cascade"
    )
    assert links[4] in text, "the first five links are rendered"
    assert links[5] not in text, "the list is bounded at five"
    assert "-# and 3 more" in text, "the remainder is counted rather than dropped silently"


def test_setup_conversations_say_none_open_when_there_are_none() -> None:
    container = build_routing_container(
        paginate([], page=0, page_size=ROUTING_PAGE_SIZE),
        server_default=None,
        deployment_default=None,
        deployment_in_effect=True,
        conversations=[],
        sentence=PRECEDENCE_LINE,
    )
    assert "-# none open" in _text(container), "an empty list gets a concrete empty state"


def test_setup_conversation_links_are_jump_links_into_the_guild() -> None:
    answering_map = _map(
        setup_threads=(
            SetupThreadRef(
                thread_id="55",
                parent_channel_id="900",
                target_name="churn-explorer",
                updated_at=_SET_AT,
            ),
            SetupThreadRef(thread_id="56", parent_channel_id="900", updated_at=_SET_AT),
        )
    )
    links = setup_conversation_links(answering_map, guild_id=2001)
    assert links == [
        "[Set up churn-explorer](https://discord.com/channels/2001/55)",
        "[Set up an agent](https://discord.com/channels/2001/56)",
    ], "each live setup thread gets a jump link, named by its target when it has one"


# ---------------------------------------------------------------------------
# The rule sentence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("is_admin", "expected_lead"),
    [(True, "Tell Daimon:"), (False, "An admin can tell Daimon:")],
)
def test_the_rule_sentence_reaches_both_roles_in_their_own_voice(
    account_id: uuid.UUID, is_admin: bool, expected_lead: str
) -> None:
    answering_map = _map(
        channels=(ChannelAnswer(channel_id="900", agent_name="research-bot"),),
        deployment_default=None,
    )
    state = _state(
        answering_map,
        account_id=account_id,
        is_admin=is_admin,
        roster=(_agent("research-bot"), _agent("churn-explorer")),
    )
    sentence = build_routing_sentence(state, answering_map)

    assert sentence.startswith(PRECEDENCE_LINE), "the precedence rule leads"
    assert expected_lead in sentence, "only the voice differs between the two roles"
    assert "Make churn-explorer answer in #growth." in sentence, (
        "the example names an agent nothing routes to, in the channel the reader is standing in"
    )


def test_the_rule_sentence_falls_back_to_the_answering_agent_when_everything_is_routed(
    account_id: uuid.UUID,
) -> None:
    answering_map = _map(
        channels=(ChannelAnswer(channel_id="900", agent_name="research-bot"),),
        deployment_default=None,
    )
    state = _state(answering_map, account_id=account_id, roster=(_agent("research-bot"),))
    assert "Make research-bot answer in #growth." in build_routing_sentence(state, answering_map), (
        "with nothing unrouted, the agent answering here is at least a name the reader knows"
    )


def test_the_rule_sentence_is_the_bare_rule_when_there_is_no_agent_to_name(
    account_id: uuid.UUID,
) -> None:
    answering_map = _map(deployment_default=None)
    state = _state(answering_map, account_id=account_id)
    assert build_routing_sentence(state, answering_map) == PRECEDENCE_LINE, (
        "an empty roster gets the rule and no invented example"
    )


# ---------------------------------------------------------------------------
# Paging and navigation
# ---------------------------------------------------------------------------


async def test_routing_paginates_beyond_the_page_size(account_id: uuid.UUID) -> None:
    lines = _lines(ROUTING_PAGE_SIZE + 5)
    state = _state(_map(), account_id=account_id, roster=(_agent("daimon"),))
    view = RoutingView(
        state,
        runtime=_make_runtime(),
        allowed_user_id=42,
        lines=lines,
        server_default=None,
    )
    first_text = _text(view)
    assert lines[ROUTING_PAGE_SIZE - 1].channel_label in first_text, "page 0 is full"
    assert lines[ROUTING_PAGE_SIZE].channel_label not in first_text, "the overflow is paged away"

    interaction = _interaction()
    await _find_button(view, "Next ▶").callback(interaction)

    interaction.response.edit_message.assert_called_once()
    second = interaction.response.edit_message.call_args.kwargs["view"]
    assert isinstance(second, RoutingView), "Next must swap in another routing page"
    assert state.routing_page == 1, "the page advances on the shared state"
    second_text = _text(second)
    assert lines[ROUTING_PAGE_SIZE].channel_label in second_text, "page 1 shows the overflow"
    assert lines[0].channel_label not in second_text, "page 1 does not repeat page 0"


def test_routing_carries_no_pager_when_everything_fits(account_id: uuid.UUID) -> None:
    state = _state(_map(), account_id=account_id)
    view = RoutingView(
        state, runtime=_make_runtime(), allowed_user_id=42, lines=_lines(3), server_default=None
    )
    labels = {node.label for node in _walk(view) if isinstance(node, discord.ui.Button)}
    assert "Next ▶" not in labels, "a single page needs no pager"
    assert labels == {"◀ Back", "Done"}, "the screen offers only Back and Done"


async def test_back_returns_to_the_roster_page_the_reader_left(account_id: uuid.UUID) -> None:
    from daimon.adapters.discord.agent_setup.roster_view import RosterView

    roster = tuple(_agent(f"bot-{index}") for index in range(9))
    state = _state(_map(), account_id=account_id, roster=roster, roster_page=1)
    view = RoutingView(
        state, runtime=_make_runtime(), allowed_user_id=42, lines=[], server_default=None
    )
    interaction = _interaction()

    await _find_button(view, "◀ Back").callback(interaction)

    swapped = interaction.response.edit_message.call_args.kwargs["view"]
    assert isinstance(swapped, RosterView), "Back returns to the roster"
    assert swapped.state is state, "Back reuses the panel state rather than refetching"
    assert state.roster_page == 1, "the roster page the reader left is preserved"


def test_routing_offers_no_setup_button(account_id: uuid.UUID) -> None:
    """Setup targets an agent; this screen selects none, so offering it would mislead."""
    state = _state(_map(), account_id=account_id, roster=(_agent("daimon"),))
    view = RoutingView(
        state, runtime=_make_runtime(), allowed_user_id=42, lines=[], server_default=None
    )
    labels = {node.label for node in _walk(view) if isinstance(node, discord.ui.Button)}
    assert not any(label is not None and "Set up" in label for label in labels), (
        "Who answers where carries no setup button"
    )


# ---------------------------------------------------------------------------
# The ported audit resolution
# ---------------------------------------------------------------------------


async def test_load_routing_lines_names_channels_and_resolves_who_set_them(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """The audit line joins the recorded account to a live Discord mention."""
    await make_tenant(db_session, platform="discord", workspace_id="guild-routing", id=tenant_id)
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant_id, platform="discord", external_id="7788"
    )
    await db_session.commit()

    guild = MagicMock(spec=discord.Guild)
    named_channel = MagicMock()
    named_channel.name = "data"
    guild.get_channel = MagicMock(side_effect=lambda cid: named_channel if cid == 900 else None)

    answering_map = _map(
        channels=(
            ChannelAnswer(
                channel_id="900",
                agent_name="research-bot",
                set_by_account_id=principal.account_id,
                set_at=_SET_AT,
            ),
            ChannelAnswer(channel_id="901", agent_name="churn-explorer"),
        ),
        tenant_default=TenantAnswer(agent_name="daimon", set_at=_SET_AT),
    )

    lines, server_default = await load_routing_lines(db_session, guild, answering_map)

    assert [line.channel_label for line in lines] == ["#data", "#901"], (
        "a cached channel is named; a cache miss falls back to its id"
    )
    assert lines[0].audit_line == f"set by <@7788> on <t:{int(_SET_AT.timestamp())}:d>", (
        "the audit line names the Discord principal and the date it was set"
    )
    assert lines[1].audit_line is None, (
        "a row with neither an actor nor a timestamp gets no invented audit line"
    )
    assert server_default is not None, "a workspace default must come back as its own line"
    assert server_default.agent_name == "daimon"
    assert server_default.audit_line == f"set on <t:{int(_SET_AT.timestamp())}:d>", (
        "a timestamp with no recorded actor still says when"
    )
