"""Teams setup cards, rendered from the core read models."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime

from daimon.adapters.teams import setup_card
from daimon.core.agent_details import AgentDetails
from daimon.core.answering_map import AnsweringMap, ChannelAnswer, ChannelEnvironment
from daimon.core.roster import paginate
from daimon.core.scope import AnsweringPlace


def _environments_text(
    answering_map: AnsweringMap, channel_names: Mapping[str, str] | None = None
) -> str:
    """The Who answers where card from its Environments heading on."""
    page = paginate((), page=1, page_size=setup_card.PAGE_SIZE)
    card = setup_card.routing_card(
        answering_map, page, is_admin=True, request_agent=None, channel_names=channel_names
    )
    text = json.dumps(card.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False)
    return text.partition("**Environments**")[2]


def test_who_answers_where_lists_each_channels_environment_and_the_defaults() -> None:
    """As on Discord and Slack, the routing card shows where each channel's turns run."""
    rows = tuple(
        ChannelEnvironment(channel_id=f"19:c{index}@thread.tacv2", environment_name=f"env-{index}")
        for index in range(setup_card.MAX_ENVIRONMENT_LINES + 2)
    )
    text = _environments_text(
        AnsweringMap(
            channel_environments=rows,
            tenant_environment="org-env",
            deployment_environment="base",
        ),
        {"19:c0@thread.tacv2": "General"},
    )
    assert "General → **env-0**" in text, "a channel's own environment, by its name"
    assert "19:c0" not in text, "never the channel's id"
    assert "env-11" not in text and "…and 2 more" in text, "lines past the cap fold"
    assert "**Organisation default:** org-env" in text, "the organisation default"
    assert "**Daimon-wide default:** base (not in effect" in text, "shadowed by the org default"


def test_who_answers_where_names_each_channel_and_never_shows_its_id() -> None:
    """The id in a code span rendered as a boxed block with the agent dangling below it."""
    known, unknown = "19:dAaXWo7raNuqUnDmoEGV0zInbE5F@thread.tacv2", "19:gone@thread.tacv2"
    overrides = (
        ChannelAnswer(channel_id=known, agent_name="qa-haiku"),
        ChannelAnswer(channel_id=unknown, agent_name="qa-sonnet"),
    )
    page = paginate(overrides, page=1, page_size=setup_card.PAGE_SIZE)
    card = setup_card.routing_card(
        AnsweringMap(channel_overrides=overrides),
        page,
        is_admin=True,
        request_agent=None,
        channel_names={known: "Q*A <room>"},
    )
    text = json.dumps(card.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False)

    assert "Q\\\\*A room → **qa-haiku**" in text, "the stored name, as literal text"
    assert "Channel name unavailable → **qa-sonnet**" in text, "no name known: a neutral label"
    assert "19:" not in text and "`" not in text, "no id and no code span"


def test_who_answers_where_says_when_no_channel_picks_an_environment() -> None:
    text = _environments_text(AnsweringMap(deployment_environment="base"))
    assert "No channel picks its own environment yet." in text, "the empty state is stated"
    assert "**Organisation default:** Not assigned" in text, "no organisation environment"
    assert "**Daimon-wide default:** base" in text and "not in effect" not in text, (
        "the deployment default is in effect with no organisation default"
    )


def test_only_admins_get_the_operator_tokens_dialog_and_it_offers_tenant_scopes_only() -> None:
    page = paginate((), page=1, page_size=setup_card.PAGE_SIZE)

    def card_json(is_admin: bool) -> str:
        card = setup_card.routing_card(AnsweringMap(), page, is_admin=is_admin, request_agent=None)
        return json.dumps(card.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False)

    assert setup_card.OPERATOR_DIALOG in card_json(True)
    assert setup_card.OPERATOR_DIALOG not in card_json(False), "members never see the entry"
    form = json.dumps(setup_card.operator_tokens_card([]).model_dump(by_alias=True))
    assert "tenant:read" in form and "promo:create" not in form


def test_token_card_is_a_title_then_lines_a_blank_line_apart() -> None:
    def body(channel_id: str | None) -> list[dict[str, object]]:
        card = setup_card.token_card(
            agent_name="analyst", cli="npx …", mcp_json="{}", jti="j1", channel_id=channel_id
        )
        return card.model_dump(by_alias=True)["body"]

    title, copy, run, *_ = body(None)
    assert (title["text"], title.get("weight")) == ("Use analyst from your coding tools", "Bolder")
    assert (copy["text"], copy.get("spacing")) == (
        "Copy this now: the key is shown only once. It acts as you.",
        "Medium",
    )
    assert run["text"] == "**Run this:**", "no channel line for a token bound to no channel"
    bound = body("19:abc@thread.tacv2")[2]
    assert (bound["text"], bound.get("spacing")) == (
        "It runs in 19:abc@thread.tacv2, with that channel's rules and budget.",
        "Medium",
    ), "the channel line only for a channel-bound token"


def test_details_names_the_channels_an_agent_answers_in_and_never_their_ids() -> None:
    here, named, unnamed = "19:here@thread.tacv2", "19:named@thread.tacv2", "19:gone@thread.tacv2"
    details = AgentDetails(
        ma_agent_id="ag_helper",
        name="Helper",
        model_id="model",
        model_display_name="Model",
        daimon_managed=False,
        created_by_is_workspace=True,
        created_at=datetime.now(UTC),
        answers_here=True,
        applies_note="Changes apply next turn.",
        answers_in=(
            AnsweringPlace(tier="channel", channel_id=here),
            AnsweringPlace(tier="channel", channel_id=named),
            AnsweringPlace(tier="channel", channel_id=unnamed),
            AnsweringPlace(tier="tenant"),
        ),
    )
    card = setup_card.details_card(
        details,
        here=here,
        page=0,
        coding_tools=False,
        is_admin=True,
        channel_names={named: "Research"},
    )
    text = json.dumps(card.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False)

    expected = "this chat, Research, Channel name unavailable, the organisation default"
    assert f"**Answers in:** {expected}" in text
    assert "19:" not in text and "`" not in text, "no id and no code span"


def test_the_agents_owner_is_offered_add_skill_and_a_member_is_not() -> None:
    details = AgentDetails(
        ma_agent_id="ag_helper",
        name="Helper",
        model_id="model",
        model_display_name="Model",
        daimon_managed=False,
        created_by_is_workspace=True,
        created_at=datetime.now(UTC),
        answers_here=False,
        applies_note="Changes apply next turn.",
        answers_in=(),
    )

    def text(*, owned: bool) -> str:
        card = setup_card.details_card(
            details,
            here="19:x@thread.tacv2",
            page=0,
            coding_tools=True,
            is_admin=False,
            owned=owned,
        )
        return json.dumps(card.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False)

    assert "Add skill" in text(owned=True)
    assert "Add skill" not in text(owned=False)
    # The coding-tool token stays an admin's.
    assert "coding tools" not in text(owned=True)
