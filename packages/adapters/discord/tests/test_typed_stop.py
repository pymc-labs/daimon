"""A typed "stop" in a running reply's thread takes the Stop button's path (J7)."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import discord
import pytest
from daimon.adapters.discord.bot import DaimonBot
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
        "stop the fleet",
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
    stop.reply.assert_awaited_once_with(STOPPED, mention_author=False)  # type: ignore[attr-defined]
    stop.add_reaction.assert_not_called()  # type: ignore[attr-defined]  # not queued
    again = _typed("<@999> cancel that", REQUESTER)
    await queued_bot.on_message(again)
    again.reply.assert_awaited_once_with(ALREADY_STOPPING, mention_author=False)  # type: ignore[attr-defined]
    finish.set()
    await task
    assert calls == ["write a long story"], "a stop never becomes a turn of its own"


@pytest.mark.asyncio
async def test_someone_else_typing_stop_is_told_who_can(queued_bot: DaimonBot) -> None:
    task, cancel, finish, _ = await _running_turn(queued_bot)
    stop = _typed("<@999> stop", STRANGER)
    await queued_bot.on_message(stop)
    assert not cancel.is_set()
    stop.reply.assert_awaited_once_with(STOP_NOT_ALLOWED, mention_author=False)  # type: ignore[attr-defined]
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


def _live_fleet(bot: DaimonBot, text: str) -> AsyncMock:
    """The thread has a still-running fleet run; its card answers a typed stop with `text`."""
    card = AsyncMock()
    card.stop_typed.return_value = text
    bot._live_fleet_card = AsyncMock(return_value=(card, AsyncMock()))  # type: ignore[method-assign]
    return card.stop_typed


@pytest.mark.asyncio
async def test_stop_the_fleet_stops_its_workers_and_the_running_reply(
    queued_bot: DaimonBot,
) -> None:
    from daimon.adapters.discord.posted_controls.fleet import FLEET_STOPPING, TypedPresser

    task, cancel, finish, calls = await _running_turn(queued_bot)
    stop_typed = _live_fleet(queued_bot, FLEET_STOPPING)
    stop = _typed("<@999> stop the fleet", REQUESTER)
    await queued_bot.on_message(stop)
    (presser,) = stop_typed.await_args.args
    assert isinstance(presser, TypedPresser) and presser.user is stop.author
    assert cancel.is_set(), "the root reply stops with its workers"
    stop.reply.assert_awaited_once_with(FLEET_STOPPING, mention_author=False)  # type: ignore[attr-defined]
    finish.set()
    await task
    assert calls == ["write a long story"]


@pytest.mark.asyncio
async def test_stop_with_a_live_fleet_and_no_running_reply_starts_no_turn(
    queued_bot: DaimonBot,
) -> None:
    from daimon.adapters.discord.posted_controls.fleet import FLEET_STOPPING

    turns: list[str] = []

    async def turn(message: discord.Message, guild_id: str, tenant_id: uuid.UUID, **_: object):
        turns.append(message.content)

    queued_bot._handle_mention = turn  # type: ignore[method-assign]
    stop_typed = _live_fleet(queued_bot, FLEET_STOPPING)
    stop = _typed("<@999> stop", REQUESTER)
    await queued_bot.on_message(stop)
    stop_typed.assert_awaited_once()
    stop.reply.assert_awaited_once_with(FLEET_STOPPING, mention_author=False)  # type: ignore[attr-defined]
    assert turns == []


@pytest.mark.asyncio
async def test_a_member_who_did_not_start_the_fleet_is_told_who_can_stop_it(
    queued_bot: DaimonBot,
) -> None:
    refusal = "Only the person who started this fleet, or a server admin, can stop it."
    task, cancel, finish, _ = await _running_turn(queued_bot)
    _live_fleet(queued_bot, refusal)
    stop = _typed("<@999> stop the fleet", STRANGER)
    await queued_bot.on_message(stop)
    assert not cancel.is_set(), "a refused fleet stop leaves the reply running too"
    stop.reply.assert_awaited_once_with(refusal, mention_author=False)  # type: ignore[attr-defined]
    cancel.set()
    finish.set()
    await task


@pytest.mark.asyncio
async def test_the_fleet_s_starter_cannot_stop_someone_else_s_reply(queued_bot: DaimonBot) -> None:
    """A stops their fleet; member B's running reply in the thread keeps going."""
    from daimon.adapters.discord.posted_controls.fleet import FLEET_STOPPING

    task, cancel, finish, _ = await _running_turn(queued_bot, requester=STRANGER)
    stop_typed = _live_fleet(queued_bot, FLEET_STOPPING)
    stop = _typed("<@999> stop the fleet", REQUESTER)
    await queued_bot.on_message(stop)
    stop_typed.assert_awaited_once()
    assert not cancel.is_set(), "B's reply is B's (or an admin's) to stop"
    stop.reply.assert_awaited_once_with(FLEET_STOPPING, mention_author=False)  # type: ignore[attr-defined]
    cancel.set()
    finish.set()
    await task


@pytest.mark.asyncio
async def test_someone_refused_the_fleet_still_stops_their_own_reply(queued_bot: DaimonBot) -> None:
    """B didn't start the fleet, so it keeps running; B's own reply stops."""
    task, cancel, finish, _ = await _running_turn(queued_bot, requester=STRANGER)
    _live_fleet(
        queued_bot, "Only the person who started this fleet, or a server admin, can stop it."
    )
    stop = _typed("<@999> stop", STRANGER)
    await queued_bot.on_message(stop)
    assert cancel.is_set()
    stop.reply.assert_awaited_once_with(STOPPED, mention_author=False)  # type: ignore[attr-defined]
    finish.set()
    await task


@pytest.mark.asyncio
async def test_a_non_starter_is_refused_from_the_stored_card_before_any_discord_call(
    queued_bot: DaimonBot,
) -> None:
    from daimon.adapters.discord.posted_controls.fleet import FLEET_STOP_NOT_ALLOWED

    queued_bot._live_fleet_card = AsyncMock(return_value="not_allowed")  # type: ignore[method-assign]
    stop = _typed("<@999> stop the fleet", STRANGER)
    await queued_bot.on_message(stop)
    stop.reply.assert_awaited_once_with(FLEET_STOP_NOT_ALLOWED, mention_author=False)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_an_unmentioned_stop_never_touches_a_fleet(queued_bot: DaimonBot) -> None:
    """ "never mind" between people stops at most the speaker's own running reply."""
    from daimon.adapters.discord.posted_controls.fleet import FLEET_STOPPING

    task, cancel, finish, _ = await _running_turn(queued_bot)
    stop_typed = _live_fleet(queued_bot, FLEET_STOPPING)
    stop = _typed("never mind", REQUESTER)
    assert await queued_bot._typed_stop(stop, stop.channel, mentioned=False)  # pyright: ignore[reportPrivateUsage]
    stop_typed.assert_not_awaited()
    queued_bot._live_fleet_card.assert_not_awaited()  # type: ignore[attr-defined]
    assert cancel.is_set()
    other = _typed("never mind", STRANGER)
    assert not await queued_bot._typed_stop(other, other.channel, mentioned=False)  # pyright: ignore[reportPrivateUsage]
    other.reply.assert_not_called()  # type: ignore[attr-defined]
    finish.set()
    await task


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
