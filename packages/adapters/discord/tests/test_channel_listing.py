"""Discord lists, from its cache, the text and forum channels and threads the bot can see."""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
from daimon.adapters.discord.names import listed_guild_channels


def _channel(kind: type, channel_id: int, name: str, *, visible: bool = True) -> MagicMock:
    channel = MagicMock(spec=kind)
    channel.id, channel.name = channel_id, name
    channel.permissions_for.return_value.view_channel = visible
    return channel


def test_only_visible_text_forum_and_thread_channels_are_listed() -> None:
    category = MagicMock(spec=discord.CategoryChannel)
    category.id, category.name = 9, "Projects"
    text = _channel(discord.TextChannel, 1, "general")
    text.category = None
    forum = _channel(discord.ForumChannel, 2, "ideas")
    forum.category = category
    hidden = _channel(discord.TextChannel, 3, "staff", visible=False)
    voice = _channel(discord.VoiceChannel, 4, "lounge")
    thread = _channel(discord.Thread, 5, "launch plan")
    thread.parent, thread.archived = text, True
    guild = MagicMock(spec=discord.Guild)
    guild.channels, guild.threads = [text, forum, hidden, voice], [thread]

    found = listed_guild_channels(guild)
    assert {k: (v.kind, v.parent_id, v.archived) for k, v in found.items()} == {
        "1": ("text", None, False),
        "2": ("forum", "9", False),
        "5": ("thread", "1", True),
    }
