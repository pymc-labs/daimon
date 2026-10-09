"""Setup panels and forms from before picture uploads were turned off get the refusal."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from daimon.adapters.discord.agent_setup.stale_picture import (
    STALE_CHANGE_ID,
    StalePictureChangeButton,
    is_stale_picture_submit,
    refuse_stale_picture_submit,
)
from daimon.adapters.discord.bot import DaimonBot

_REFUSAL = "Custom pictures are turned off."


def _picture_submit(
    *, custom_id: str = "0f" * 16, content_type: str = "image/png"
) -> dict[str, Any]:
    """The submit payload of the old picture form: a note, then one file field."""
    return {
        "custom_id": custom_id,
        "components": [
            {"type": 10, "id": 1},
            {
                "type": 18,
                "id": 2,
                "component": {"type": 19, "id": 3, "custom_id": "ab" * 16, "values": ["555"]},
            },
        ],
        "resolved": {"attachments": {"555": {"id": "555", "content_type": content_type}}},
    }


def _interaction(data: dict[str, Any]) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.type = discord.InteractionType.modal_submit
    interaction.data = data
    interaction.guild_id = None
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    return interaction


def test_old_change_button_id_matches_the_registered_template() -> None:
    template = StalePictureChangeButton.__discord_ui_compiled_template__
    assert template.fullmatch(STALE_CHANGE_ID)
    assert not template.fullmatch("agent-setup:avatar-reset")


async def test_old_change_button_click_only_sends_the_refusal() -> None:
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    item = await StalePictureChangeButton.from_custom_id(interaction, MagicMock(), MagicMock())

    with patch("daimon.core.stores.agent_avatars.replace_avatar") as replace_avatar:
        await item.callback(interaction)

    interaction.response.send_message.assert_awaited_once_with(_REFUSAL, ephemeral=True)
    replace_avatar.assert_not_called()


@pytest.mark.parametrize(
    ("data", "live", "expected"),
    [
        (_picture_submit(), set[str](), True),
        (_picture_submit(custom_id="live"), {"live"}, False),
        (_picture_submit(content_type="text/plain"), set[str](), False),
        (
            {
                "custom_id": "x",
                "components": [
                    {"type": 18, "component": {"type": 4, "custom_id": "t", "value": "hi"}}
                ],
            },
            set[str](),
            False,
        ),
    ],
    ids=["stale-picture-form", "live-form", "not-an-image", "text-form"],
)
def test_recognises_only_an_unknown_one_image_form(
    data: dict[str, Any], live: set[str], expected: bool
) -> None:
    assert is_stale_picture_submit(data, live_modal_ids=live) is expected


async def test_bot_answers_an_old_picture_form_with_the_refusal_and_writes_nothing() -> None:
    bot = MagicMock()
    bot._connection._view_store._modals = {}
    interaction = _interaction(_picture_submit())

    with (
        patch("daimon.core.stores.agent_avatars.replace_avatar") as replace_avatar,
        patch("daimon.adapters.discord.bot.remember_guild_user"),
    ):
        await DaimonBot.on_interaction(bot, interaction)

    interaction.response.send_message.assert_awaited_once_with(_REFUSAL, ephemeral=True)
    replace_avatar.assert_not_called()


async def test_a_live_form_is_left_to_its_own_handler() -> None:
    interaction = _interaction(_picture_submit(custom_id="live"))

    answered = await refuse_stale_picture_submit(interaction, live_modal_ids={"live"})

    assert not answered
    interaction.response.send_message.assert_not_awaited()
