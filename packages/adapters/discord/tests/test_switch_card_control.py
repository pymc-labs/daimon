"""The switch card's buttons: only the requester's press acts, and the card shows what happened."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.posted_controls import switch_card as module
from daimon.core.switch_cards import SwitchCardPress, card_custom_id


def _control(action: str = "confirm") -> module.SwitchCardControl:
    card_id = uuid.uuid4()
    return module.SwitchCardControl(
        discord.ui.Button(label="Confirm", custom_id=card_custom_id(card_id, action))  # pyright: ignore[reportArgumentType]
    )


def _interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.guild = SimpleNamespace(id=1234)
    interaction.user = SimpleNamespace(id=42)
    interaction.message = MagicMock()
    interaction.message.edit = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.client.runtime = MagicMock()
    return interaction


def test_the_control_reads_its_card_and_action_from_the_custom_id():
    card_id = uuid.uuid4()
    control = module.SwitchCardControl(
        discord.ui.Button(label="Cancel", custom_id=card_custom_id(card_id, "cancel"))
    )
    assert (control.card_id, control.press) == (card_id, "cancel")
    with pytest.raises(ValueError):
        module.SwitchCardControl(discord.ui.Button(label="x", custom_id="fleet:nope"))


async def test_someone_elses_press_is_answered_privately_and_the_card_stays(monkeypatch):
    monkeypatch.setattr(module, "press_switch_card", AsyncMock(return_value=None))
    interaction = _interaction()
    await _control().callback(interaction)
    interaction.followup.send.assert_awaited_once_with(module.NOT_YOURS, ephemeral=True)
    interaction.message.edit.assert_not_awaited()


async def test_the_requesters_press_edits_the_card_to_what_happened(monkeypatch):
    press = SwitchCardPress("Switched Researcher to OpenAI.", closed=True, applied=True)
    pressed = AsyncMock(return_value=press)
    monkeypatch.setattr(module, "press_switch_card", pressed)
    interaction = _interaction()
    await _control().callback(interaction)
    assert pressed.await_args.kwargs["clicker"] == "42"
    interaction.message.edit.assert_awaited_once()
    view = interaction.message.edit.await_args.kwargs["view"]
    assert "Switched Researcher to OpenAI." in str(
        [getattr(item, "content", "") for item in view.walk_children()]
    )
    assert not [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]


async def test_a_failed_press_is_reported_privately(monkeypatch):
    monkeypatch.setattr(module, "press_switch_card", AsyncMock(side_effect=RuntimeError("boom")))
    interaction = _interaction()
    await _control().callback(interaction)
    interaction.followup.send.assert_awaited_once_with(module.UNAVAILABLE, ephemeral=True)
