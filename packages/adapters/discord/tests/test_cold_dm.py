"""A DM with no conversation to continue is answered once, not met with silence."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.commands import direct_messages
from daimon.adapters.discord.commands.direct_messages import DirectMessageCog

_OFF = (
    "Private chats with Daimon are off here.\n"
    "Mention me in a channel, or ask a server admin to run `/dm-me enable`."
)


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def _cog() -> DirectMessageCog:
    bot = MagicMock()
    bot.runtime.sessionmaker = lambda: _Session()
    return DirectMessageCog(bot)


def _dm(author_id: int) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.author = SimpleNamespace(id=author_id, bot=False)
    message.channel = MagicMock(spec=discord.DMChannel)
    message.channel.id = 900 + author_id
    message.channel.send = AsyncMock()
    return message


def _no_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(direct_messages, "get_conversation", AsyncMock(return_value=None))


def _conversation_with_dms_off(monkeypatch: pytest.MonkeyPatch) -> None:
    conversation: Any = SimpleNamespace(tenant_id="t", workspace_id="1")
    monkeypatch.setattr(direct_messages, "get_conversation", AsyncMock(return_value=conversation))
    monkeypatch.setattr(direct_messages, "dm_enabled", AsyncMock(return_value=False))


async def test_dms_off_for_a_known_server_gets_the_two_line_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _conversation_with_dms_off(monkeypatch)
    message = _dm(1)

    await _cog()._reply(message)  # pyright: ignore[reportPrivateUsage]

    message.channel.send.assert_awaited_once_with(_OFF)


async def test_a_cold_dm_names_no_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DM does not say which server the person means, so the answer does not claim
    private chats are off anywhere and names no server."""
    _no_conversation(monkeypatch)
    message = _dm(1)

    await _cog()._reply(message)  # pyright: ignore[reportPrivateUsage]

    text = message.channel.send.await_args.args[0]
    assert text.startswith("To talk here, run `/dm-me` in a server channel first")
    assert "are off here" not in text


async def test_each_person_is_answered_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    _conversation_with_dms_off(monkeypatch)
    cog = _cog()
    first, again, other = _dm(1), _dm(1), _dm(2)

    for message in (first, again, other):
        await cog._reply(message)  # pyright: ignore[reportPrivateUsage]

    first.channel.send.assert_awaited_once()
    again.channel.send.assert_not_awaited()
    other.channel.send.assert_awaited_once()


async def test_a_failed_answer_is_retried_on_the_next_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _conversation_with_dms_off(monkeypatch)
    cog = _cog()
    failing = _dm(1)
    failing.channel.send.side_effect = discord.Forbidden(MagicMock(status=403), "Cannot send")
    retry = _dm(1)

    await cog._reply(failing)  # pyright: ignore[reportPrivateUsage]
    await cog._reply(retry)  # pyright: ignore[reportPrivateUsage]

    retry.channel.send.assert_awaited_once_with(_OFF)
