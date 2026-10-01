"""/dm move refuses a sealed source before reading any of its history."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.commands import direct_messages
from daimon.adapters.discord.commands.direct_messages import DirectMessageCog


def _sealed_admission() -> MagicMock:
    # A sealed source as admit() reports it; memory is read-only there too.
    return MagicMock(source_sealed=True, memory_read_only=True)


def _history(*_: Any, **__: Any) -> Any:
    async def gen() -> Any:
        message = MagicMock()
        message.content = "sealed client detail"
        message.author.id = 5
        message.author.display_name = "someone"
        yield message

    return gen()


@pytest.mark.parametrize("channel_type", [discord.TextChannel, discord.Thread])
async def test_dm_move_from_a_sealed_channel_refuses_before_history(
    monkeypatch: pytest.MonkeyPatch, channel_type: type[Any]
) -> None:
    monkeypatch.setattr(direct_messages, "require_dm_enabled", AsyncMock())
    monkeypatch.setattr(direct_messages, "admit", AsyncMock(return_value=_sealed_admission()))
    start_dm = AsyncMock()
    monkeypatch.setattr(direct_messages, "start_dm", start_dm)

    channel = MagicMock(spec=channel_type)
    channel.id = 200
    channel.parent_id = 100
    channel.history = MagicMock(side_effect=_history)
    member = MagicMock()
    member.id = 7
    member.guild_permissions.administrator = False
    member.guild_permissions.manage_guild = False
    member.create_dm = AsyncMock()
    guild = MagicMock()
    guild.id = 1
    guild.owner_id = 99
    guild.fetch_member = AsyncMock(return_value=member)
    interaction = MagicMock()
    interaction.guild = guild
    interaction.channel = channel
    interaction.user.id = 7
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    cog = DirectMessageCog(MagicMock())
    await DirectMessageCog.dm.callback.__wrapped__(cog, interaction, "move")  # pyright: ignore[reportFunctionMemberAccess]

    channel.history.assert_not_called()
    member.create_dm.assert_not_called()
    start_dm.assert_not_called()
    interaction.followup.send.assert_awaited_once()
    assert "sealed" in interaction.followup.send.await_args.args[0]


async def test_dm_move_drops_system_notices_such_as_a_sealed_threads_created_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread-created notice in the parent carries the (sealed) thread's name."""
    monkeypatch.setattr(direct_messages, "require_dm_enabled", AsyncMock())
    monkeypatch.setattr(
        direct_messages,
        "admit",
        AsyncMock(return_value=MagicMock(source_sealed=False, memory_read_only=False)),
    )
    start_dm = AsyncMock()
    monkeypatch.setattr(direct_messages, "start_dm", start_dm)

    def message(content: str, kind: discord.MessageType) -> MagicMock:
        item = MagicMock()
        item.content = content
        item.type = kind
        item.author.id = 5
        item.author.display_name = "someone"
        return item

    history = [
        message("Acme pricing negotiation", discord.MessageType.thread_created),
        message("ordinary message", discord.MessageType.default),
        message("a reply", discord.MessageType.reply),
    ]

    def _pages(*_: Any, **__: Any) -> Any:
        async def gen() -> Any:
            for item in history:
                yield item

        return gen()

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 100
    channel.history = MagicMock(side_effect=_pages)
    member = MagicMock()
    member.id = 7
    member.guild_permissions.administrator = False
    member.guild_permissions.manage_guild = False
    dm_channel = MagicMock()
    dm_channel.id = 900
    dm_channel.send = AsyncMock()
    member.create_dm = AsyncMock(return_value=dm_channel)
    guild = MagicMock()
    guild.id = 1
    guild.owner_id = 99
    guild.fetch_member = AsyncMock(return_value=member)
    interaction = MagicMock()
    interaction.guild = guild
    interaction.channel = channel
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    bot = MagicMock()
    bot.user = None
    cog = DirectMessageCog(bot)

    await DirectMessageCog.dm.callback.__wrapped__(cog, interaction, "move")  # pyright: ignore[reportFunctionMemberAccess]

    start_dm.assert_awaited_once()
    texts = [turn.text for turn in start_dm.await_args.kwargs["context"]]
    assert not any("Acme pricing" in text for text in texts)
    assert any("ordinary message" in text for text in texts)
    assert any("a reply" in text for text in texts)
    assert start_dm.await_args.kwargs["source_channel_id"] == "100"
    assert start_dm.await_args.kwargs["source_thread_id"] is None
