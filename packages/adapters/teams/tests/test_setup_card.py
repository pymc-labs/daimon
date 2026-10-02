"""Teams setup cards, rendered from the core read models."""

from __future__ import annotations

import json

from daimon.adapters.teams import setup_card
from daimon.core.answering_map import AnsweringMap, ChannelEnvironment
from daimon.core.roster import paginate


def _environments_text(answering_map: AnsweringMap) -> str:
    """The Who answers where card from its Environments heading on."""
    page = paginate((), page=1, page_size=setup_card.PAGE_SIZE)
    card = setup_card.routing_card(answering_map, page, is_admin=True, request_agent=None)
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
        )
    )
    assert "Channel `19:c0@thread.tacv2`: **env-0**" in text, "a channel's own environment"
    assert "env-11" not in text and "…and 2 more" in text, "lines past the cap fold"
    assert "**Organisation default:** org-env" in text, "the organisation default"
    assert "**Deployment default:** base (not in effect" in text, "shadowed by the org default"


def test_who_answers_where_says_when_no_channel_picks_an_environment() -> None:
    text = _environments_text(AnsweringMap(deployment_environment="base"))
    assert "No channel picks its own environment yet." in text, "the empty state is stated"
    assert "**Organisation default:** Not assigned" in text, "no organisation environment"
    assert "**Deployment default:** base" in text and "not in effect" not in text, (
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
