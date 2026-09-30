"""This channel's environment on Who answers where: what it shows, who may pick, what a pick stores."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from daimon.adapters.slack.agent_setup import actions
from daimon.adapters.slack.agent_setup.actions import (
    _dispatch_panel_action,  # pyright: ignore[reportPrivateUsage]
    load_routing_view,
)
from daimon.adapters.slack.agent_setup.channel_environment import ENVIRONMENT_NEED_ADMIN_MESSAGE
from daimon.adapters.slack.agent_setup.panel_views import (
    ACTION_ENVIRONMENT,
    MAX_ENVIRONMENT_LINES,
    build_routing_view,
)
from daimon.adapters.slack.agent_setup.state import PANEL_PAGE_SIZE, PanelMetadata
from daimon.core.answering_map import AnsweringMap, ChannelEnvironment
from daimon.core.channel_environments import (
    ENVIRONMENT_OPTION_INHERIT,
    EnvironmentPicker,
    environment_option_value,
)
from daimon.core.roster import paginate
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.scoped_config_read import get_scope
from daimon.testing import ma_environment
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TEAM = "T0ENVS"
CHANNEL = "C0GROWTH"
USER = "U0LEAD"
META = PanelMetadata(team_id=TEAM, channel_id=CHANNEL, view="routing")


def _routing(answering_map: AnsweringMap, picker: EnvironmentPicker | None) -> dict[str, Any]:
    return build_routing_view(
        answering_map,
        page=paginate((), page=0, page_size=PANEL_PAGE_SIZE),
        meta=META,
        is_admin=picker is not None,
        attributions={},
        setup_links=[],
        channel_id=CHANNEL,
        unrouted_agent_name=None,
        environment_picker=picker,
    )


def _text(view: dict[str, Any]) -> str:
    return json.dumps(view["blocks"], ensure_ascii=False)


def _select(view: dict[str, Any]) -> dict[str, Any] | None:
    blocks: list[dict[str, Any]] = view["blocks"]
    for block in blocks:
        elements: list[dict[str, Any]] = block.get("elements") or []
        for element in elements:
            if element.get("action_id") == ACTION_ENVIRONMENT:
                return element
    return None


# ---------------------------------------------------------------------------
# What the view shows
# ---------------------------------------------------------------------------


def test_the_block_lists_channels_then_the_defaults_and_the_select_marks_the_pick() -> None:
    view = _routing(
        AnsweringMap(
            channel_environments=(ChannelEnvironment(channel_id=CHANNEL, environment_name="gpu"),),
            tenant_environment="shared",
            deployment_environment="default",
        ),
        EnvironmentPicker(channel_id=CHANNEL, own="gpu", inherited="shared", names=("gpu",)),
    )
    text = _text(view)
    select = _select(view)

    assert f"<#{CHANNEL}> → *gpu*" in text, "a channel line reads channel → environment"
    assert text.index("*Workspace default:* shared") < text.index(
        "Deployment default: *default*"
    ), "the workspace default comes before the deployment fall-through"
    assert "_not in effect while a workspace default is set_" in text, (
        "a workspace default takes the deployment default out of the cascade"
    )
    assert select is not None, "an admin gets the select"
    assert [o["value"] for o in select["options"]] == [
        ENVIRONMENT_OPTION_INHERIT,
        environment_option_value("gpu"),
    ], "the default leads, then each environment"
    assert select["options"][0]["text"]["text"] == "Use the default (shared)", (
        "the first option names what the channel falls to"
    )
    assert select["initial_option"]["value"] == environment_option_value("gpu"), (
        "the channel's own pick is pre-selected"
    )


def test_with_nothing_set_the_block_names_only_the_deployment_default() -> None:
    text = _text(_routing(AnsweringMap(deployment_environment="default"), None))

    assert "_no channel picks its own environment yet_" in text, "no channel rows"
    assert "*Workspace default:* Not assigned" in text, "no tenant row"
    assert "Deployment default: *default*" in text and "not in effect" not in text, (
        "with no rows at all, every channel runs where it did before"
    )


def test_the_listing_fits_one_section_with_long_names_full_of_markup() -> None:
    long_name = "&<" * 300
    rows = tuple(
        ChannelEnvironment(channel_id=f"C{index:09}", environment_name=long_name)
        for index in range(MAX_ENVIRONMENT_LINES)
    )
    view = _routing(
        AnsweringMap(
            channel_environments=rows,
            tenant_environment=long_name,
            deployment_environment=long_name,
        ),
        None,
    )
    sections = [
        block["text"]["text"]
        for block in view["blocks"]
        if block["type"] == "section" and block["text"]["text"].startswith("*Environments*")
    ]

    assert len(sections) == 1 and len(sections[0]) <= 3000, "within Slack's section cap"
    text = sections[0]
    assert "more_" in text, "the channel lines cut are counted"
    assert "*Workspace default:* &amp;&lt;" in text and "Deployment default:" in text, (
        "both defaults show, escaped"
    )
    assert not text.endswith("…"), "the section is budgeted, not truncated mid-entity"


# ---------------------------------------------------------------------------
# Who gets the select, and what a pick stores
# ---------------------------------------------------------------------------


def _runtime(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, calls: list[str]
) -> MagicMock:
    router = MARouter()
    router.add_agent_list()
    router.add_environment_list(
        ma_environment(id="env_science", name="science", tenant_id=tenant_id)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return router.dispatch(request)

    runtime = MagicMock()
    runtime.sessionmaker = sessionmaker
    runtime.anthropic = build_fake_anthropic(handler)
    runtime.deployment_default = DeploymentDefault(agent_name="daimon", environment_name="default")
    return runtime


def _client() -> MagicMock:
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    client.views_update = AsyncMock()
    return client


async def _seed(sessionmaker: async_sessionmaker[AsyncSession], *, grant: bool) -> uuid.UUID:
    async with sessionmaker() as session, session.begin():
        tenant = await make_tenant(session, platform="slack", workspace_id=TEAM)
        if grant:
            await set_channel_admins(
                session,
                tenant_id=tenant.id,
                platform="slack",
                channel_id=CHANNEL,
                role_ids=[],
                user_ids=[USER],
                actor_account_id=None,
            )
    return tenant.id


async def _pick(runtime: MagicMock, client: MagicMock, tenant_id: uuid.UUID, value: str) -> None:
    await _dispatch_panel_action(
        runtime,
        client,
        {"view": {"id": "V1", "hash": "H1"}},
        action={"action_id": ACTION_ENVIRONMENT, "selected_option": {"value": value}},
        action_id=ACTION_ENVIRONMENT,
        meta=META,
        tenant_id=tenant_id,
        team_id=TEAM,
        user_id=USER,
    )


async def test_members_get_no_select_and_no_environment_listing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _seed(db_session_factory, grant=False)
    calls: list[str] = []

    view = await load_routing_view(
        _runtime(db_session_factory, tenant_id, calls),
        tenant_id=tenant_id,
        meta=META,
        is_admin=False,
        user_id=USER,
    )

    assert _select(view) is None, "a member with no grant for this channel cannot pick"
    assert "/v1/environments" not in calls, "and costs no environment listing"


async def test_a_channel_admin_picks_this_channels_environment_and_hands_it_back(
    db_session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actions, "resolve_is_admin", AsyncMock(return_value=False))
    tenant_id = await _seed(db_session_factory, grant=True)
    runtime = _runtime(db_session_factory, tenant_id, [])
    client = _client()
    scope = ChannelScopeRef(tenant_id=tenant_id, channel_id=CHANNEL)

    await _pick(runtime, client, tenant_id, environment_option_value("science"))

    row = await get_scope(db_session, scope=scope)
    assert row is not None and row.environment_name == "science", "the pick is stored"
    refreshed = client.views_update.call_args.kwargs["view"]
    assert f"<#{CHANNEL}> → *science*" in _text(refreshed), "the view shows the pick"
    assert _select(refreshed) is not None, "the channel admin keeps the select"
    note = client.chat_postEphemeral.call_args.kwargs["text"]
    assert "now runs in the science environment" in note, "the reader is told what changed"

    await _pick(runtime, client, tenant_id, ENVIRONMENT_OPTION_INHERIT)

    assert await get_scope(db_session, scope=scope) is None, (
        "handing the channel back leaves no row, exactly as before any pick"
    )


@pytest.mark.parametrize(
    ("grant", "value", "expected"),
    [
        (False, environment_option_value("science"), ENVIRONMENT_NEED_ADMIN_MESSAGE),
        (True, environment_option_value("gone"), "no longer exists"),
        (True, "science", "Nothing changed"),
    ],
    ids=["member", "vanished-environment", "forged-value"],
)
async def test_a_refused_or_stale_pick_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    grant: bool,
    value: str,
    expected: str,
) -> None:
    monkeypatch.setattr(actions, "resolve_is_admin", AsyncMock(return_value=False))
    tenant_id = await _seed(db_session_factory, grant=grant)
    client = _client()

    await _pick(_runtime(db_session_factory, tenant_id, []), client, tenant_id, value)

    assert expected in client.chat_postEphemeral.call_args.kwargs["text"], (
        "the reader is told why nothing changed"
    )
    assert (
        await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=CHANNEL))
        is None
    ), "nothing is written"
