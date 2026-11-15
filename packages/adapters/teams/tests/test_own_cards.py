"""Presses on Daimon's own cards in Teams: the press carries who, where and which card."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.teams import own_cards as module
from daimon.adapters.teams.card_actions import Actor
from daimon.core import host_calls
from daimon.core.host_calls import APPROVED_TEXT, WAITING_TEXT, CallPress, card_custom_id
from microsoft_teams.api import AdaptiveCardActionCardResponse, AdaptiveCardActionMessageResponse

_TENANT = uuid.uuid4()
_ACTOR = Actor(user_id="user-1", tenant_id=_TENANT, is_admin=False, conversation_id="19:c")


def _ctx(custom_id: str, reply_to_id: str | None = "activity-1") -> SimpleNamespace:
    data = {"action": module.VERB, "custom_id": custom_id}
    activity = SimpleNamespace(
        value=SimpleNamespace(action=SimpleNamespace(data=data)), reply_to_id=reply_to_id
    )
    return SimpleNamespace(activity=activity)


@pytest.fixture
def cards(monkeypatch: pytest.MonkeyPatch) -> module.TeamsOwnCards:
    monkeypatch.setattr(module, "card_actor", AsyncMock(return_value=_ACTOR))
    monkeypatch.setattr(module, "deployment_on", lambda *_args: True)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    return module.TeamsOwnCards(SimpleNamespace(sessionmaker=None, settings=None))  # pyright: ignore[reportArgumentType]


async def test_a_closing_press_names_the_clicker_message_and_tenant_and_replaces_the_card(
    monkeypatch: pytest.MonkeyPatch, cards: module.TeamsOwnCards
) -> None:
    """Core checks requester, message and tenant, so the handler passes exactly those."""
    pressed = AsyncMock(return_value=CallPress(APPROVED_TEXT, closed=True, approved=True))
    monkeypatch.setattr(host_calls, "press_host_call", pressed)
    card_id = uuid.uuid4()

    got = await cards.on_action(_ctx(card_custom_id(card_id, "approve")))  # pyright: ignore[reportArgumentType]

    assert pressed.await_args is not None, "the press reached the core"
    kwargs = pressed.await_args.kwargs
    assert (kwargs["clicker"], kwargs["message_id"], kwargs["card_id"], kwargs["tenant_id"]) == (
        "user-1",
        "activity-1",
        card_id,
        _TENANT,
    ), "the press names the verified clicker, the card's activity, the card and the tenant"
    assert isinstance(got, AdaptiveCardActionCardResponse), "the card shows the outcome"
    assert APPROVED_TEXT in got.model_dump_json(), "with the outcome's words"


@pytest.mark.parametrize(
    ("press", "said"),
    [(None, module.NOT_YOURS), (CallPress(WAITING_TEXT, closed=False), WAITING_TEXT)],
)
async def test_a_press_that_closes_nothing_only_toasts(
    monkeypatch: pytest.MonkeyPatch,
    cards: module.TeamsOwnCards,
    press: CallPress | None,
    said: str,
) -> None:
    """Someone else's press, or one the core refused, leaves the card as it is."""
    monkeypatch.setattr(host_calls, "press_host_call", AsyncMock(return_value=press))

    got = await cards.on_action(_ctx(card_custom_id(uuid.uuid4(), "deny")))  # pyright: ignore[reportArgumentType]

    assert isinstance(got, AdaptiveCardActionMessageResponse), "a toast, not a new card"
    assert got.value == said, "the presser hears why"


async def test_an_unverified_clicker_or_message_never_reaches_the_core(
    monkeypatch: pytest.MonkeyPatch, cards: module.TeamsOwnCards
) -> None:
    """No verified clicker, or no card message, refuses before any press."""
    pressed = AsyncMock()
    monkeypatch.setattr(host_calls, "press_host_call", pressed)

    got = await cards.on_action(_ctx(card_custom_id(uuid.uuid4(), "approve"), reply_to_id=None))  # pyright: ignore[reportArgumentType]

    assert isinstance(got, AdaptiveCardActionMessageResponse), "refused with a toast"
    assert pressed.await_count == 0, "the core was never asked"
