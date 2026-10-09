"""Setup panels from before picture uploads were turned off get the refusal."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.discord.agent_setup.stale_picture import (
    STALE_CHANGE_ID,
    StalePictureChangeButton,
)

_REFUSAL = "Custom pictures are turned off."


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
