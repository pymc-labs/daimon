"""A click on a switch card posted before switches applied at once closes it as expired."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.posted_controls.switch_card import SwitchCardControl
from daimon.core.switch_cards import EXPIRED_TEXT


async def test_an_older_switch_card_button_is_parsed_and_answered_as_expired():
    custom_id = f"bsw:{uuid.uuid4()}:confirm"
    match = SwitchCardControl.__discord_ui_compiled_template__.fullmatch(custom_id)
    assert match is not None
    item = discord.ui.Button(label="Confirm", custom_id=custom_id)
    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    control = await SwitchCardControl.from_custom_id(interaction, item, match)

    await control.callback(interaction)

    view = interaction.response.edit_message.await_args.kwargs["view"]
    assert EXPIRED_TEXT in str([getattr(i, "content", "") for i in view.walk_children()])
    assert not [i for i in view.walk_children() if isinstance(i, discord.ui.Button)]


def test_any_other_custom_id_is_not_a_switch_card_control():
    with pytest.raises(ValueError):
        SwitchCardControl(discord.ui.Button(custom_id="hcc:nope"))
