"""Block Kit rendering of a posted card."""

from __future__ import annotations

from typing import Any

import pytest
from daimon.core.credential_requests import SLACK_ACTION_ID
from daimon.core.posted_controls.cards import (
    FOOTER_TEMPLATE,
    CardButton,
    CardKind,
    CardState,
    PostedCard,
)
from daimon.core.posted_controls.slack_blocks import (
    MAX_CONTEXT_ELEMENTS,
    build_card_blocks,
    card_notification_text,
)

from .test_cards import AGENT, EXPIRES, KINDS, REQUESTER, STATES, TOKEN, build

EXPIRES_UNIX = int(EXPIRES.timestamp())


def blocks_of(kind: CardKind, state: CardState, **overrides: Any) -> list[dict[str, Any]]:
    return build_card_blocks(build(kind, state, **overrides), token=TOKEN)


def test_requested_card_renders_section_facts_actions_and_footer() -> None:
    blocks = blocks_of("env", "requested")

    assert [block["type"] for block in blocks] == ["section", "context", "actions", "context"], (
        "a requested card is headline, facts, buttons, footer — in that order"
    )
    assert blocks[0]["text"] == {
        "type": "mrkdwn",
        "text": f"*🔑 Add TOGGL_TOKEN to {AGENT}*",
    }, "the headline is the bold section text"
    assert [element["text"] for element in blocks[1]["elements"]] == [
        f"Anyone who talks to {AGENT} can use it.",
        "The value is not shown in chat.",
    ], "each fact becomes its own context element"


def test_private_form_button_carries_the_token_in_its_value() -> None:
    blocks = blocks_of("env", "requested")
    (element,) = blocks[2]["elements"]

    assert element["action_id"] == SLACK_ACTION_ID, "Slack routes the click by action_id"
    assert element["value"] == TOKEN, "the opaque request token travels in the button's value"
    assert element["style"] == "primary", "the form button is the primary action"
    assert element["text"] == {
        "type": "plain_text",
        "text": "🔐 Enter it privately",
        "emoji": True,
    }, "the button label is plain text with emoji enabled"


def test_requested_footer_names_the_requester_and_a_live_expiry() -> None:
    blocks = blocks_of("env", "requested")
    (element,) = blocks[3]["elements"]

    assert element["text"] == (
        f"Only <@{REQUESTER}> can open this form. "
        f"Expires <!date^{EXPIRES_UNIX}^{{time}}|17:30 UTC>."
    ), "the footer substitutes the mention and Slack's live-date token into the template"
    assert "{requester}" not in element["text"], (
        "no placeholder may survive into the rendered block"
    )


def test_received_footer_renders_verbatim() -> None:
    blocks = blocks_of("env", "received")

    assert blocks[-1]["type"] == "context", "the received footer is a context block"
    assert blocks[-1]["elements"][0]["text"] == "Received. Saving…", (
        "a footer without placeholders is shown as written"
    )


@pytest.mark.parametrize("state", [state for state in STATES if state != "requested"])
def test_only_a_requested_card_renders_an_actions_block(state: CardState) -> None:
    blocks = blocks_of("env", state)

    assert all(block["type"] != "actions" for block in blocks), (
        f"a {state} card must not offer anything to click"
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("state", STATES)
def test_every_card_renders_well_formed_blocks(kind: CardKind, state: CardState) -> None:
    blocks = blocks_of(kind, state)

    assert blocks[0]["type"] == "section", f"{kind}/{state} must open with the headline section"
    for block in blocks:
        assert "type" in block, f"{kind}/{state} produced a block with no type"
        if block["type"] == "context":
            assert 1 <= len(block["elements"]) <= MAX_CONTEXT_ELEMENTS, (
                f"{kind}/{state} context blocks must stay inside Slack's element cap"
            )


def test_a_long_refusal_folds_into_one_context_element() -> None:
    reasons = tuple(f"Line {n}: no = sign." for n in range(1, 16))

    blocks = blocks_of("env_file", "refused", refusal="env_file_invalid", refusal_lines=reasons)
    (element,) = blocks[1]["elements"]

    assert element["text"] == "\n".join(reasons), (
        "more reasons than Slack allows elements are folded together, never dropped"
    )


def test_a_card_with_no_facts_renders_no_context_block() -> None:
    card = PostedCard(kind="env", state="expired", headline="⌛ This form expired.")

    blocks = build_card_blocks(card)

    assert [block["type"] for block in blocks] == ["section"], (
        "a headline-only card is a single section"
    )


def test_a_form_button_without_its_token_is_refused() -> None:
    card = build("env", "requested")

    with pytest.raises(ValueError, match="token"):
        build_card_blocks(card)


def test_a_link_button_carries_its_url_and_no_value() -> None:
    card = PostedCard(
        kind="repo",
        state="requested",
        headline=f"📦 Give {AGENT} access to acme/analytics",
        buttons=(
            CardButton(label="Connect GitHub", style="link", url="https://example.invalid/i"),
        ),
        footer=FOOTER_TEMPLATE,
        requester_platform_user_id=REQUESTER,
        expires_at_unix=EXPIRES_UNIX,
    )

    blocks = build_card_blocks(card)
    (element,) = blocks[1]["elements"]

    assert element["url"] == "https://example.invalid/i", (
        "a link button sends the person to its URL"
    )
    assert "value" not in element, "a link button carries no request token"
    assert element["action_id"] != SLACK_ACTION_ID, (
        "a link button must not collide with the form button's action_id"
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("state", STATES)
def test_notification_text_is_the_headline(kind: CardKind, state: CardState) -> None:
    card = build(kind, state)

    assert card_notification_text(card) == card.headline, (
        "the notification fallback is the card's headline"
    )
    assert card_notification_text(card).strip(), f"{kind}/{state} must have a non-empty fallback"
