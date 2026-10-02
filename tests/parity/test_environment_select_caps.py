"""Executable record: the environment select's length differs by platform.

Both panels plan the select with the same core picker and lead with Use the
default, then list environments up to what the platform's select holds:
Discord 25 options, Slack 100. A tenant with more environments than that sees
a shorter list on Discord; the chat tools name any of them on both. If either
cap changes, update this record rather than deleting it.
"""

from __future__ import annotations

from typing import Any

from daimon.adapters.discord.agent_setup import channel_environment as discord_env
from daimon.adapters.slack.agent_setup import channel_environment as slack_env
from daimon.adapters.slack.agent_setup.panel_views import ACTION_ENVIRONMENT, build_routing_view
from daimon.adapters.slack.agent_setup.state import PANEL_PAGE_SIZE, PanelMetadata
from daimon.core.answering_map import AnsweringMap
from daimon.core.channel_environments import EnvironmentPicker, plan_environment_picker
from daimon.core.roster import paginate

_NAMES = [f"env-{index:03}" for index in range(150)]


def _picker(limit: int) -> EnvironmentPicker:
    picker = plan_environment_picker(
        AnsweringMap(), channel_id="c1", names=_NAMES, limit=limit, max_value_length=100
    )
    assert picker is not None, "a tenant with environments gets a picker"
    return picker


def test_discord_offers_24_environments_after_the_default() -> None:
    select = discord_env.build_environment_select(
        _picker(discord_env.MAX_ENVIRONMENT_OPTIONS), channel_name="general"
    )
    assert len(select.options) == 25, "Discord's select cap"


def test_slack_offers_99_environments_after_the_default() -> None:
    view = build_routing_view(
        AnsweringMap(),
        page=paginate((), page=0, page_size=PANEL_PAGE_SIZE),
        meta=PanelMetadata(team_id="T1", channel_id="C1", view="routing"),
        is_admin=True,
        attributions={},
        setup_links=[],
        channel_id="C1",
        unrouted_agent_name=None,
        environment_picker=_picker(slack_env.MAX_ENVIRONMENT_OPTIONS),
    )
    blocks: list[dict[str, Any]] = view["blocks"]
    elements: list[dict[str, Any]] = [e for b in blocks for e in b.get("elements") or []]
    select = next(e for e in elements if e.get("action_id") == ACTION_ENVIRONMENT)
    assert len(select["options"]) == 100, "Slack's static select cap"
