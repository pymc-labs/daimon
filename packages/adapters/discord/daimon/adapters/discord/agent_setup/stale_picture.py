"""Refuse picture uploads from setup panels and forms opened before uploads were turned off.

The old Change button kept its fixed custom_id, so a class-level `DynamicItem`
catches a click on it after a restart. The old upload form had a random
custom_id, so discord.py drops its submit as unknown; `refuse_stale_picture_submit`
recognises it by shape instead: one file field holding an image, from a form
this process never opened. Both only answer with the refusal and write nothing.
"""

from __future__ import annotations

import re
from collections.abc import Container
from typing import Any, Self, cast

from daimon.core.agent_identity import CUSTOM_PICTURES_OFF

import discord
from discord.ext import commands

STALE_CHANGE_ID = "agent-setup:avatar-change"
_TEXT_DISPLAY = 10
_LABEL = 18
_FILE_UPLOAD = 19


class StalePictureChangeButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.LayoutView]],
    template=re.escape(STALE_CHANGE_ID),
):
    """The Change button on a setup panel rendered before uploads were turned off."""

    def __init__(self) -> None:
        super().__init__(discord.ui.Button(label="Change", custom_id=STALE_CHANGE_ID))

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # discord.py's ClientT is a free TypeVar; this adapter only ever runs DaimonBot
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls()

    async def callback(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        await interaction.response.send_message(CUSTOM_PICTURES_OFF, ephemeral=True)


def is_stale_picture_submit(data: dict[str, Any], *, live_modal_ids: Container[str]) -> bool:
    """Pure: whether a modal submit is the old picture upload form.

    The form held a note and one file field. A live form with the same shape
    (the `.env` key upload) is still registered, so only an unknown custom_id
    counts, and only when the file is an image.
    """
    if str(data.get("custom_id") or "") in live_modal_ids:
        return False
    rows = [
        cast(dict[str, Any], row)
        for row in cast(list[object], data.get("components") or [])
        if isinstance(row, dict)
    ]
    fields = [row for row in rows if row.get("type") != _TEXT_DISPLAY]
    if len(fields) != 1 or fields[0].get("type") != _LABEL:
        return False
    field = cast(dict[str, Any], fields[0].get("component") or {})
    values = cast(list[object], field.get("values") or [])
    if field.get("type") != _FILE_UPLOAD or len(values) != 1:
        return False
    resolved = cast(dict[str, Any], data.get("resolved") or {})
    attachments = cast(dict[str, Any], resolved.get("attachments") or {})
    attachment = cast(dict[str, Any], attachments.get(str(values[0])) or {})
    return str(attachment.get("content_type") or "").startswith("image/")


async def refuse_stale_picture_submit(
    interaction: discord.Interaction[Any], *, live_modal_ids: Container[str]
) -> bool:
    """Answer an old picture upload form with the refusal. True if it answered."""
    if interaction.type is not discord.InteractionType.modal_submit:
        return False
    data = cast(dict[str, Any], interaction.data if isinstance(interaction.data, dict) else {})
    if not is_stale_picture_submit(data, live_modal_ids=live_modal_ids):
        return False
    if interaction.response.is_done():
        return False
    await interaction.response.send_message(CUSTOM_PICTURES_OFF, ephemeral=True)
    return True
