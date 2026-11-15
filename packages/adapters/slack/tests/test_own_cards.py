"""Presses on Daimon's own cards in Slack: the press carries who, where and which card."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack import own_cards as module
from daimon.core import host_calls
from daimon.core.host_calls import APPROVED_TEXT, WAITING_TEXT, CallPress, card_custom_id
from daimon.core.ma_identity import derive_tenant_uuid

_TEAM = "T1"
_USER = "U1"


def _payload(action_id: str) -> dict[str, Any]:
    return {
        "team": {"id": _TEAM},
        "user": {"id": _USER},
        "channel": {"id": "C1"},
        "container": {"message_ts": "1700.2", "thread_ts": "1700.1"},
        "actions": [{"action_id": action_id}],
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    web = MagicMock()
    web.chat_update = AsyncMock()
    web.chat_postEphemeral = AsyncMock()
    monkeypatch.setattr(module, "resolve_web_client", AsyncMock(return_value=web))
    monkeypatch.setattr(module, "deployment_on", lambda *_args: True)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    return web


async def test_a_closing_press_names_the_clicker_message_and_tenant_and_edits_the_card(
    monkeypatch: pytest.MonkeyPatch, client: MagicMock
) -> None:
    """Core checks requester, message and tenant, so the handler passes exactly those."""
    pressed = AsyncMock(return_value=CallPress(APPROVED_TEXT, closed=True, approved=True))
    monkeypatch.setattr(host_calls, "press_host_call", pressed)
    card_id = uuid.uuid4()

    await module.handle_own_card_click(
        SimpleNamespace(sessionmaker=None, settings=None),  # pyright: ignore[reportArgumentType]
        _payload(card_custom_id(card_id, "approve")),
    )

    assert pressed.await_args is not None, "the press reached the core"
    kwargs = pressed.await_args.kwargs
    assert (kwargs["clicker"], kwargs["message_id"], kwargs["card_id"]) == (
        _USER,
        "1700.2",
        card_id,
    ), "the press names the clicker, the card's message and the card"
    assert kwargs["tenant_id"] == derive_tenant_uuid(platform="slack", workspace_id=_TEAM)
    assert client.chat_update.await_args.kwargs["ts"] == "1700.2", "the card itself is edited"
    assert APPROVED_TEXT in str(client.chat_update.await_args.kwargs["blocks"]), "shows outcome"


@pytest.mark.parametrize(
    ("press", "said"),
    [(None, module.NOT_YOURS), (CallPress(WAITING_TEXT, closed=False), WAITING_TEXT)],
)
async def test_a_press_that_closes_nothing_answers_only_the_presser(
    monkeypatch: pytest.MonkeyPatch, client: MagicMock, press: CallPress | None, said: str
) -> None:
    """Someone else's press, or one the core refused, leaves the card as it is."""
    monkeypatch.setattr(host_calls, "press_host_call", AsyncMock(return_value=press))

    await module.handle_own_card_click(
        SimpleNamespace(sessionmaker=None, settings=None),  # pyright: ignore[reportArgumentType]
        _payload(card_custom_id(uuid.uuid4(), "deny")),
    )

    assert client.chat_update.await_count == 0, "the card is untouched"
    kwargs = client.chat_postEphemeral.await_args.kwargs
    assert (kwargs["user"], said in str(kwargs)) == (_USER, True), "only the presser hears why"


async def test_a_failed_press_still_answers_the_presser(
    monkeypatch: pytest.MonkeyPatch, client: MagicMock
) -> None:
    """A crash in the press must not leave the click hanging."""
    monkeypatch.setattr(host_calls, "press_host_call", AsyncMock(side_effect=RuntimeError))

    await module.handle_own_card_click(
        SimpleNamespace(sessionmaker=None, settings=None),  # pyright: ignore[reportArgumentType]
        _payload(card_custom_id(uuid.uuid4(), "approve")),
    )

    assert module.UNAVAILABLE in str(client.chat_postEphemeral.await_args.kwargs), "told why"


def test_only_call_and_switch_ids_route_here() -> None:
    """Every other action id keeps its own handler."""
    assert module.is_own_card_action("hcc:x") and module.is_own_card_action("bsw:x"), "routed"
    assert not module.is_own_card_action("dcf:x"), "a tool confirmation is not ours"
