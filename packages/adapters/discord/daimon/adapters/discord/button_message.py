"""A hand-over click presented to normal mention ingress as the clicker's message."""

from typing import Any

import discord


class ButtonMessage(discord.Message):
    """A fresh input id, with cosmetic reactions attached to the clicked notice.

    Copy the SDK message's transport/channel state, discarding cached content.
    The input is attributed to the clicker; no fields of the notice are mutated.
    """

    def __init__(
        self,
        notice: discord.Message,
        *,
        author: discord.User | discord.Member,
        click_id: int,
        text: str,
    ) -> None:
        for cls in discord.Message.__mro__:
            for slot in getattr(cls, "__slots__", ()):
                if not slot.startswith("_cs_") and hasattr(notice, slot):
                    setattr(self, slot, getattr(notice, slot))
        self.id = click_id
        self.author = author
        self.content = text
        self.attachments = []
        self.embeds = []
        self.mentions = []
        self.role_mentions = []
        self.reference = None
        self.webhook_id = None
        self.application_id = None
        self.mention_everyone = False
        self._edited_timestamp = None
        self._notice = notice

    @property
    def jump_url(self) -> str:
        return self._notice.jump_url

    async def add_reaction(
        self, emoji: discord.Emoji | discord.PartialEmoji | discord.Reaction | str, /
    ) -> None:
        await self._notice.add_reaction(emoji)

    async def remove_reaction(
        self,
        emoji: discord.Emoji | discord.PartialEmoji | discord.Reaction | str,
        member: discord.abc.Snowflake,
    ) -> None:
        await self._notice.remove_reaction(emoji, member)

    async def reply(
        self,
        content: str | None = None,
        **kwargs: Any,  # noqa: ANN401 — SDK send options pass through unchanged
    ) -> discord.Message:
        return await self._notice.reply(content, **kwargs)
