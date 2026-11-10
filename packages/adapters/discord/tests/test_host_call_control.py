"""The host call card's buttons: the press carries who, where and which card, and the card shows the outcome."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.posted_controls import host_call as module
from daimon.core.host_calls import (
    APPROVED_TEXT,
    DENIED_TEXT,
    CallPress,
    card_custom_id,
)
from daimon.core.ma_identity import derive_tenant_uuid

_GUILD = 1234
_USER = 42
_MESSAGE = 777


def _control(action: str = "approve") -> module.HostCallControl:
    custom_id = card_custom_id(uuid.uuid4(), action)  # pyright: ignore[reportArgumentType]
    return module.HostCallControl(discord.ui.Button(label=action, custom_id=custom_id))  # pyright: ignore[reportArgumentType]


def _interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.guild = SimpleNamespace(id=_GUILD)
    interaction.user = SimpleNamespace(id=_USER)
    interaction.message = MagicMock()
    interaction.message.id = _MESSAGE
    interaction.message.edit = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.client.runtime = MagicMock()
    return interaction


def _gate(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    def deployment_on(settings: object, key: str, platform: str) -> bool:
        return on

    monkeypatch.setattr(module, "deployment_on", deployment_on)


def _card_text(interaction: MagicMock) -> str:
    view = interaction.message.edit.await_args.kwargs["view"]
    return str([getattr(item, "content", "") for item in view.walk_children()])


def test_the_control_reads_its_card_and_action_from_the_custom_id() -> None:
    """A button survives a restart: the card and the action come back from its custom id."""
    card_id = uuid.uuid4()
    control = module.HostCallControl(
        discord.ui.Button(label="Deny", custom_id=card_custom_id(card_id, "deny"))  # pyright: ignore[reportArgumentType]
    )
    assert (control.card_id, control.press) == (card_id, "deny"), "id and action are read back"
    with pytest.raises(ValueError):
        module.HostCallControl(discord.ui.Button(label="x", custom_id="invalid:nope"))  # pyright: ignore[reportArgumentType]


async def test_a_press_names_the_clicker_the_message_and_the_servers_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core checks requester, message and tenant, so the handler must pass exactly those."""
    pressed = AsyncMock(return_value=CallPress(APPROVED_TEXT, closed=True, approved=True))
    monkeypatch.setattr(module, "press_host_call", pressed)
    _gate(monkeypatch, on=True)
    control = _control("approve")

    await control.callback(_interaction())

    assert pressed.await_args is not None, "the press reached the core"
    kwargs = pressed.await_args.kwargs
    assert kwargs["clicker"] == str(_USER), "the clicker is the Discord user"
    assert kwargs["message_id"] == str(_MESSAGE), "the card is the message that was clicked"
    assert kwargs["tenant_id"] == derive_tenant_uuid(
        platform="discord", workspace_id=str(_GUILD)
    ), "the tenant is this server's"
    assert (kwargs["card_id"], kwargs["action"]) == (control.card_id, "approve"), (
        "the button's call"
    )
    assert kwargs["enabled"] is True, "the feature switch is read from settings"


async def test_someone_elses_press_is_answered_privately_and_the_card_stays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A press that is not the requester's, or not this message or tenant, comes back empty."""
    monkeypatch.setattr(module, "press_host_call", AsyncMock(return_value=None))
    _gate(monkeypatch, on=True)
    interaction = _interaction()

    await _control().callback(interaction)

    interaction.followup.send.assert_awaited_once_with(
        embed=build_notice_embed(module.NOT_YOURS), ephemeral=True
    )
    interaction.message.edit.assert_not_awaited()


@pytest.mark.parametrize(
    ("action", "press", "shown"),
    [
        ("approve", CallPress(APPROVED_TEXT, closed=True, approved=True), APPROVED_TEXT),
        ("deny", CallPress(DENIED_TEXT, closed=True, approved=False), DENIED_TEXT),
    ],
)
async def test_an_answered_card_is_edited_to_what_happened_without_buttons(
    monkeypatch: pytest.MonkeyPatch, action: str, press: CallPress, shown: str
) -> None:
    """Approve and Deny each replace the buttons with the outcome."""
    monkeypatch.setattr(module, "press_host_call", AsyncMock(return_value=press))
    _gate(monkeypatch, on=True)
    interaction = _interaction()

    await _control(action).callback(interaction)

    interaction.message.edit.assert_awaited_once()
    assert shown in _card_text(interaction), "the card says what was decided"
    view = interaction.message.edit.await_args.kwargs["view"]
    assert not [i for i in view.walk_children() if isinstance(i, discord.ui.Button)], (
        "an answered card has no buttons left"
    )


async def test_a_press_that_does_not_close_the_card_answers_privately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E.g. the gate was turned off: only the presser hears, the card stays as it is."""
    press = CallPress("These cards are off right now.", closed=False)
    monkeypatch.setattr(module, "press_host_call", AsyncMock(return_value=press))
    _gate(monkeypatch, on=False)
    interaction = _interaction()

    await _control().callback(interaction)

    interaction.followup.send.assert_awaited_once_with(
        embed=build_notice_embed(press.text, neutral=True), ephemeral=True
    )
    interaction.message.edit.assert_not_awaited()


async def test_a_failed_press_is_reported_privately(monkeypatch: pytest.MonkeyPatch) -> None:
    """A press that errors answers the click instead of leaving it hanging."""
    monkeypatch.setattr(module, "press_host_call", AsyncMock(side_effect=RuntimeError("boom")))
    _gate(monkeypatch, on=True)
    interaction = _interaction()

    await _control().callback(interaction)

    interaction.followup.send.assert_awaited_once_with(
        embed=build_notice_embed(module.UNAVAILABLE), ephemeral=True
    )


async def test_a_press_with_no_server_or_message_is_unavailable() -> None:
    """A click outside a server's message cannot be tied to a card."""
    interaction = _interaction()
    interaction.guild = None

    await _control().callback(interaction)

    interaction.response.send_message.assert_awaited_once_with(
        embed=build_notice_embed(module.UNAVAILABLE), ephemeral=True
    )
