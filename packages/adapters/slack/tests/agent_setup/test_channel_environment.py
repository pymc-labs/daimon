"""This channel's environment on Who answers where: what it shows, who may pick, what a pick stores."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
from daimon.adapters.slack.agent_setup.actions import (
    _dispatch_panel_action,  # pyright: ignore[reportPrivateUsage]
)
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
from daimon.core.scope import DeploymentDefault
from daimon.testing.ma_models import ma_environment
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
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
        "Daimon-wide default: *default*"
    ), "the workspace default comes before the deployment fall-through"
    assert "_Not used while a workspace default is set._" in text, (
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
    assert "Daimon-wide default: *default*" in text and "not in effect" not in text, (
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
    assert "*Workspace default:* &amp;&lt;" in text and "Daimon-wide default:" in text, (
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
