"""A typed "stop" in a running reply's thread takes the Stop button's path (J7)."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import discord
import pytest
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.typed_stop import (
    ALREADY_STOPPING,
    STOPPED,
    TypedStops,
    is_stop_request,
)
from daimon.adapters.discord.views import STOP_NOT_ALLOWED

from .test_mention_queue import _make_thread_message  # pyright: ignore[reportPrivateUsage]
from .test_mention_queue import queued_bot as queued_bot

REQUESTER = 111
STRANGER = 222


@pytest.mark.parametrize(
    "text",
    [
        "stop",
        "Stop!",
        "<@999> stop",
        "<@!999> cancel that.",
        "please stop",
        "  never mind  ",
        "Cancel, please",
    ],
)
def test_a_bare_stop_phrase_is_a_stop_request(text: str) -> None:
    assert is_stop_request(text, bot_id=999)


@pytest.mark.parametrize(
    "text",
    [
        "stop using tables in your answer",
        "can you cancel my subscription?",
        "stopwatch",
        "",
        "<@123> stop",  # a message to someone else
        "<@999> <@123> stop",
    ],
)
def test_a_longer_message_is_not_a_stop_request(text: str) -> None:
    assert not is_stop_request(text, bot_id=999)


def test_only_the_requester_or_an_admin_stops_once() -> None:
    stops, cancel = TypedStops(), asyncio.Event()
    assert stops.stop(1, author_id=REQUESTER, is_admin=False) == "no_running_turn"
    stops.watch(1, requester_id=REQUESTER, cancel=cancel)
    assert stops.stop(1, author_id=STRANGER, is_admin=False) == "not_allowed"
    assert not cancel.is_set()
    assert stops.stop(1, author_id=STRANGER, is_admin=True) == "stopped"
    assert cancel.is_set()
    assert stops.stop(1, author_id=REQUESTER, is_admin=False) == "already_stopping"
    stops.release(1)
    assert stops.stop(1, author_id=REQUESTER, is_admin=False) == "no_running_turn"


async def _running_turn(
    bot: DaimonBot, *, requester: int = REQUESTER
) -> tuple[asyncio.Task[None], asyncio.Event, asyncio.Event, list[str]]:
    """A thread's turn: it stops when its cancel event is set, winding down until `finish`."""
    started, calls = asyncio.Event(), []
    cancel, finish = asyncio.Event(), asyncio.Event()

    async def turn(message: discord.Message, guild_id: str, tenant_id: uuid.UUID, **_: object):
        calls.append(message.content)
        bot._typed_stops.watch(  # pyright: ignore[reportPrivateUsage]
            message.channel.id, requester_id=message.author.id, cancel=cancel
        )
        started.set()
        await cancel.wait()
        await finish.wait()

    bot._handle_mention = turn  # type: ignore[method-assign]
    task = asyncio.create_task(
        bot.on_message(_make_thread_message(content="write a long story", author_id=requester))
    )
    await started.wait()
    return task, cancel, finish, calls


def _typed(content: str, author_id: int) -> discord.Message:
    message = _make_thread_message(content=content, author_id=author_id)
    message.reply = AsyncMock()  # type: ignore[method-assign]
    return message


@pytest.mark.asyncio
async def test_the_requester_typing_stop_stops_the_running_reply(queued_bot: DaimonBot) -> None:
    task, cancel, finish, calls = await _running_turn(queued_bot)
    stop = _typed("<@999> stop", REQUESTER)
    await queued_bot.on_message(stop)
    assert cancel.is_set()
    stop.reply.assert_awaited_once_with(
        embed=build_notice_embed(STOPPED, neutral=True), mention_author=False
    )  # type: ignore[attr-defined]
    stop.add_reaction.assert_not_called()  # type: ignore[attr-defined]  # not queued
    again = _typed("<@999> cancel that", REQUESTER)
    await queued_bot.on_message(again)
    again.reply.assert_awaited_once_with(
        embed=build_notice_embed(ALREADY_STOPPING, neutral=True), mention_author=False
    )  # type: ignore[attr-defined]
    finish.set()
    await task
    assert calls == ["write a long story"], "a stop never becomes a turn of its own"


@pytest.mark.asyncio
async def test_someone_else_typing_stop_is_told_who_can(queued_bot: DaimonBot) -> None:
    task, cancel, finish, _ = await _running_turn(queued_bot)
    stop = _typed("<@999> stop", STRANGER)
    await queued_bot.on_message(stop)
    assert not cancel.is_set()
    stop.reply.assert_awaited_once_with(
        embed=build_notice_embed(STOP_NOT_ALLOWED), mention_author=False
    )  # type: ignore[attr-defined]
    cancel.set()
    finish.set()
    await task


@pytest.mark.asyncio
async def test_a_longer_message_still_queues_behind_the_reply(queued_bot: DaimonBot) -> None:
    task, cancel, finish, calls = await _running_turn(queued_bot)
    follow_up = _typed("<@999> stop using tables", REQUESTER)
    await queued_bot.on_message(follow_up)
    assert not cancel.is_set()
    follow_up.reply.assert_not_called()  # type: ignore[attr-defined]
    follow_up.add_reaction.assert_awaited_with("⌛")  # type: ignore[attr-defined]
    cancel.set()
    finish.set()
    await task
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_stop_addressed_to_someone_else_is_an_ordinary_message(
    queued_bot: DaimonBot,
) -> None:
    task, cancel, finish, calls = await _running_turn(queued_bot)
    to_alice = _typed("<@999> <@123> stop", REQUESTER)
    await queued_bot.on_message(to_alice)
    assert not cancel.is_set()
    to_alice.reply.assert_not_called()  # type: ignore[attr-defined]
    to_alice.add_reaction.assert_awaited_with("⌛")  # type: ignore[attr-defined]
    cancel.set()
    finish.set()
    await task
    assert len(calls) == 2
