"""Switch on a conversation model card from before picks applied at once."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

from daimon.adapters.discord.stale_thread_switch import (
    ALREADY_RUNS,
    STALE_TEMPLATE,
    StaleThreadSwitchButton,
)
from daimon.adapters.discord.theme import COLOR_BLURPLE

_OLD_ID = "tmr:12:" + "a" * 64


async def test_an_old_switch_button_says_the_pick_already_applies():
    """The old card's button is caught after a restart and answers with a neutral card."""
    match = re.fullmatch(STALE_TEMPLATE, _OLD_ID)
    assert match is not None, "an old card's custom_id is matched"
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    item = await StaleThreadSwitchButton.from_custom_id(interaction, MagicMock(), match)
    await item.callback(interaction)
    sent = interaction.response.send_message.await_args
    assert sent is not None, "the press is answered"
    assert sent.kwargs["ephemeral"] is True, "only the presser sees it"
    embed = sent.kwargs["embed"]
    assert embed.description == ALREADY_RUNS, embed.description
    assert embed.color.value == COLOR_BLURPLE, "information, not a failure"
