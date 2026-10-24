"""Offline checks on the payload catalogue. No network; runs in normal CI.

Every catalogue entry must build, be JSON-serializable and pass the local rules in
`local_rules.py`. The rule tests below mutate real payloads to show each rule fires,
so a rule that silently stopped matching would fail here rather than go unnoticed.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

import pytest

from .catalogue import CATALOGUE, Built
from .local_rules import (
    check,
    discord_unique_custom_ids,
    slack_button_urls_are_http,
    slack_input_elements_have_action_id,
    slack_input_modal_has_submit,
    slack_message_has_no_top_level_text,
    slack_option_text_within_limit,
    slack_unique_ids,
)

_CACHE: dict[str, Built] = {}


def build(name: str) -> Built:
    """Build once per process; builders are deterministic and some are slow."""
    if name not in _CACHE:
        _CACHE[name] = CATALOGUE[name]()
    return _CACHE[name]


def test_catalogue_covers_both_platforms_and_every_slack_kind() -> None:
    built = [build(name) for name in CATALOGUE]
    assert {(b.platform, b.kind) for b in built} >= {
        ("slack", "view"),
        ("slack", "blocks"),
        ("slack", "message"),
        ("discord", "message"),
    }
    assert len(CATALOGUE) >= 55


@pytest.mark.parametrize("name", sorted(CATALOGUE))
def test_entry_builds_a_json_payload(name: str) -> None:
    built = build(name)
    assert built.platform in ("slack", "discord")
    assert built.payload, f"{name} built an empty payload"
    json.dumps(built.payload)  # the platform receives JSON; anything else fails at send time


@pytest.mark.parametrize("name", sorted(CATALOGUE))
def test_entry_passes_local_rules(name: str) -> None:
    assert check(build(name)) == []


# ---------------------------------------------------------------------------
# Each rule fires on a deliberately broken copy of a real payload.
# ---------------------------------------------------------------------------


def broken(name: str, mutate: Callable[[Any], None]) -> Built:
    original = build(name)
    payload = copy.deepcopy(original.payload)
    mutate(payload)
    return Built(original.platform, original.kind, payload)


def feedback_inputs(view: dict[str, Any]) -> list[dict[str, Any]]:
    return [block for block in view["blocks"] if block["type"] == "input"]


def test_rule_duplicate_block_id_fires() -> None:
    def mutate(view: Any) -> None:
        first, second = feedback_inputs(view)
        second["block_id"] = first["block_id"]

    assert any(
        "duplicate block_id" in p for p in slack_unique_ids(broken("slack/feedback_modal", mutate))
    )


def test_rule_same_action_id_in_different_blocks_is_allowed() -> None:
    """Slack accepts it (the agent list gives every row's accessory one action_id)."""
    assert slack_unique_ids(build("slack/agent_setup_agents_view")) == []


def test_rule_duplicate_action_id_inside_one_actions_block_fires() -> None:
    def mutate(blocks: Any) -> None:
        elements = blocks[0]["elements"]
        elements[1]["action_id"] = elements[0]["action_id"]

    assert any(
        "duplicate action_id" in p
        for p in slack_unique_ids(broken("slack/vote_buttons_only", mutate))
    )


def test_rule_input_without_action_id_fires() -> None:
    def mutate(view: Any) -> None:
        del feedback_inputs(view)[0]["element"]["action_id"]

    assert slack_input_elements_have_action_id(broken("slack/feedback_modal", mutate))


def test_rule_input_modal_without_submit_fires() -> None:
    assert slack_input_modal_has_submit(
        broken("slack/feedback_modal", lambda view: view.pop("submit"))
    )


def test_rule_input_modal_without_inputs_needs_no_submit() -> None:
    assert slack_input_modal_has_submit(build("slack/privacy_loading")) == []


def test_rule_option_text_over_75_fires_and_75_passes() -> None:
    def option_text(view: Any) -> dict[str, Any]:
        return feedback_inputs(view)[0]["element"]["options"][0]["text"]

    def at_limit(view: Any) -> None:
        option_text(view)["text"] = "x" * 75

    def over_limit(view: Any) -> None:
        option_text(view)["text"] = "x" * 76

    assert slack_option_text_within_limit(broken("slack/feedback_modal", at_limit)) == []
    assert slack_option_text_within_limit(broken("slack/feedback_modal", over_limit))


@pytest.mark.parametrize("field", ["initial_options", "initial_option"])
def test_rule_initial_option_text_over_75_fires(field: str) -> None:
    option = {"text": {"type": "plain_text", "text": "x" * 76}, "value": "v"}

    def mutate(view: Any) -> None:
        element = feedback_inputs(view)[0]["element"]
        element[field] = [option] if field == "initial_options" else option

    assert slack_option_text_within_limit(broken("slack/feedback_modal", mutate))


@pytest.mark.parametrize(
    "url", ["not a url", "ftp://example.com/x", "javascript:alert(1)", "https://"]
)
def test_rule_bad_button_url_fires(url: str) -> None:
    def mutate(blocks: Any) -> None:
        blocks[0]["elements"][0]["url"] = url

    assert slack_button_urls_are_http(broken("slack/vote_buttons_only", mutate))


def test_rule_good_button_url_passes() -> None:
    def mutate(blocks: Any) -> None:
        blocks[0]["elements"][0]["url"] = "https://example.com/x"

    assert slack_button_urls_are_http(broken("slack/vote_buttons_only", mutate)) == []


def test_rule_message_with_top_level_text_fires() -> None:
    assert slack_message_has_no_top_level_text(
        broken(
            "slack/here_attachment_channel", lambda message: message.update(text="Channel status")
        )
    )


def test_rule_discord_duplicate_custom_id_fires() -> None:
    def mutate(body: Any) -> None:
        buttons = body["components"][0]["components"]
        buttons.append(copy.deepcopy(buttons[0]))

    assert discord_unique_custom_ids(broken("discord/stop_button_view", mutate))
