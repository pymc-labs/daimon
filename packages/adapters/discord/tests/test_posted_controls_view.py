"""Component shape of the Discord posted-card renderer.

The card is the durable record of a private-value request, so what it draws
for each state is the contract: the button only ever exists while the request
is open, the state marker is the headline emoji rather than the accent, and
the `requested` footer is the one place a mention and an expiry are rendered
in Discord's own spellings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import discord
import pytest
from daimon.adapters.discord.posted_controls import build_card_view
from daimon.adapters.discord.theme import COLOR_AMBER
from daimon.core.continuity.messages import ChangeAvailability, ConfigurationChange
from daimon.core.posted_controls import CardButton, CardState, PostedCard, build_posted_card

_EXPIRES_AT = datetime(2026, 4, 1, 12, 30, tzinfo=UTC)
_REQUESTER = "100000000000000001"
_TOKEN = "tok-abcdefghijklmnopqrst"


def _requested_card(**overrides: Any) -> PostedCard:
    kwargs: dict[str, Any] = {
        "kind": "env",
        "state": "requested",
        "agent_name": "research-bot",
        "responder_name": "Daimon",
        "target": "TOGGL_TOKEN",
        "requester_platform_user_id": _REQUESTER,
        "expires_at": _EXPIRES_AT,
        "token": _TOKEN,
    }
    kwargs.update(overrides)
    return build_posted_card(**kwargs)


def _change(availability: ChangeAvailability) -> ConfigurationChange:
    return ConfigurationChange(
        target_name="research-bot", kind="key", detail="TOGGL_TOKEN", availability=availability
    )


def _applied_card() -> PostedCard:
    return _requested_card(
        state="applied",
        outcome=_change("next_message"),
    )


def _container(view: discord.ui.LayoutView) -> discord.ui.Container[discord.ui.LayoutView]:
    containers = [item for item in view.children if isinstance(item, discord.ui.Container)]
    assert len(containers) == 1, f"a card is exactly one container, got {view.children!r}"
    return containers[0]


def _text(view: discord.ui.LayoutView) -> list[str]:
    return [
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    ]


def _action_rows(view: discord.ui.LayoutView) -> list[discord.ui.ActionRow[Any]]:
    return [item for item in view.walk_children() if isinstance(item, discord.ui.ActionRow)]


def _buttons(view: discord.ui.LayoutView) -> list[discord.ui.Button[Any]]:
    return [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]


def test_requested_card_renders_headline_facts_button_and_footer_in_order() -> None:
    card = _requested_card()
    view = build_card_view(card)

    texts = _text(view)
    assert texts[0] == f"**{card.headline}**", "the headline leads the card, bolded"
    assert texts[1] == "\n".join(f"-# {fact}" for fact in card.facts), (
        "every fact renders as one subtext block, in the core-owned order"
    )
    rows = _action_rows(view)
    assert len(rows) == 1, "a requested card carries exactly one action row"
    buttons = _buttons(view)
    assert len(buttons) == 1, f"exactly one button on a requested card, got {buttons!r}"
    assert buttons[0].custom_id == f"ztc:{_TOKEN}", (
        "the button must carry the core-built request custom_id the bot dispatches on"
    )
    assert buttons[0].style is discord.ButtonStyle.primary, "the form button is the primary action"


def test_requested_footer_renders_the_requester_mention_and_a_relative_expiry() -> None:
    view = build_card_view(_requested_card())

    footer = _text(view)[-1]
    assert footer.startswith("-# "), "the footer renders as subtext"
    assert f"<@{_REQUESTER}>" in footer, "the footer names the requester as a real mention"
    assert f"<t:{int(_EXPIRES_AT.timestamp())}:R>" in footer, (
        "the expiry renders as Discord's relative timestamp, never a formatted string"
    )
    assert "{requester}" not in footer and "{expires}" not in footer, (
        "no template placeholder may survive into the rendered card"
    )


def test_applied_card_drops_the_action_row_and_leads_with_the_success_marker() -> None:
    view = build_card_view(_applied_card())

    assert _action_rows(view) == [], "an applied card must offer nothing left to click"
    assert _buttons(view) == [], "an applied card must carry no button"
    assert _text(view)[0].startswith("**✅"), "the applied state is marked by the headline emoji"


def test_link_and_form_buttons_coexist_in_one_action_row() -> None:
    card = _requested_card().model_copy(
        update={
            "buttons": (
                CardButton(
                    label="🔐 Enter it privately", style="primary", custom_id=f"ztc:{_TOKEN}"
                ),
                CardButton(label="What is this?", style="link", url="https://example.com/help"),
            )
        }
    )
    view = build_card_view(card)

    assert len(_action_rows(view)) == 1, "both buttons share one action row"
    buttons = _buttons(view)
    assert [b.style for b in buttons] == [
        discord.ButtonStyle.primary,
        discord.ButtonStyle.link,
    ], "button order and style follow the card's own button tuple"
    assert buttons[1].url == "https://example.com/help", "a link button carries its url"
    assert buttons[1].custom_id is None, "a link button is not dispatchable and carries no token"


def test_card_view_is_components_v2_and_never_times_out() -> None:
    view = build_card_view(_requested_card())

    assert view.has_components_v2(), (
        "the card must post as a components-v2 message; a classic view can never be "
        "edited onto it afterwards"
    )
    assert view.timeout is None, "a posted card outlives the process that built it"


@pytest.mark.parametrize(
    "card",
    [
        pytest.param(_requested_card(), id="requested"),
        pytest.param(_requested_card(state="received"), id="received"),
        pytest.param(_applied_card(), id="applied"),
        pytest.param(
            _requested_card(
                state="partial",
                outcome=_change("preparation_failed"),
            ),
            id="partial",
        ),
        pytest.param(
            _requested_card(state="refused", refusal="replacement_admin_required"), id="refused"
        ),
        pytest.param(_requested_card(state="expired"), id="expired"),
        pytest.param(_requested_card(state="superseded"), id="superseded"),
    ],
)
def test_every_card_state_wears_the_same_amber_accent(card: PostedCard) -> None:
    accent = _container(build_card_view(card)).accent_colour

    assert accent is not None and accent.value == COLOR_AMBER, (
        f"state {card.state!r} must keep the single card accent — the headline emoji is the "
        "state marker, so nothing ever turns green"
    )


@pytest.mark.parametrize(
    "state", ["applied", "partial", "refused", "expired", "superseded"], ids=str
)
def test_no_terminal_state_leaves_a_clickable_button(state: CardState) -> None:
    outcome = _change("next_message") if state in ("applied", "partial") else None
    refusal = "replacement_admin_required" if state == "refused" else None
    view = build_card_view(_requested_card(state=state, outcome=outcome, refusal=refusal))

    assert _buttons(view) == [], f"state {state!r} must not leave a button a click can reach"
