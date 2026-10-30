"""Each Teams message runs at most once, and each restart notice is sent at most once.

A process's owner key holds no lock here, so a second app sees the first as exited.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.teams import admissions as admissions_module
from daimon.adapters.teams import app as app_module
from daimon.adapters.teams.admissions import HOLDING, NOT_HELD, TeamsAdmissions
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.commands import fresh_start
from daimon.adapters.teams.identity import DENIED, TeamsInbound
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.tenants import set_provision_status
from daimon.core.turn.protection import ProtectionState
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from microsoft_teams.api import MessageActivity
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .conftest import (
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    SERVICE_URL,
    THREAD_ID,
    FakeSender,
    bot_token,
    build_teams_runtime,
    make_channel_activity,
    make_inbound,
    make_message_activity,
    shown_text,
)

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)


@pytest.fixture
async def db_session_factory(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine
) -> async_sessionmaker[AsyncSession]:
    """A connection per session, as in production: replays and turns overlap here.

    `db_session` wipes the schema first.
    """
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    await provision_tenant(factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    return factory


def _app(db_factory: async_sessionmaker[AsyncSession], sender: FakeSender) -> TeamsApp:
    runtime = build_teams_runtime(db_factory)
    return TeamsApp(
        runtime=runtime, sender=sender, commands={"new": fresh_start}, bot_token=bot_token
    )


def _message(payload: dict[str, object]) -> Any:
    activity = MessageActivity.model_validate(payload)
    return SimpleNamespace(
        activity=activity, conversation_ref=SimpleNamespace(service_url=SERVICE_URL), reply=None
    )


def _sent(
    text: str, seconds: int, activity_id: str | None = None, *, now: bool = False
) -> TeamsInbound:
    base = datetime.now(UTC) if now else datetime(2026, 1, 1, tzinfo=UTC)
    sent = base + timedelta(seconds=seconds)
    inbound = dataclasses.replace(make_inbound(text), timestamp=sent.isoformat())
    return dataclasses.replace(inbound, activity_id=activity_id) if activity_id else inbound


async def _left_by_exited_process(
    db_factory: async_sessionmaker[AsyncSession], *inbounds: TeamsInbound
) -> None:
    gate = TeamsAdmissions(db_factory, TENANT, owner_key=uuid.uuid4().int >> 66)
    for inbound in inbounds:
        assert await gate.claim(inbound, now=datetime.now(UTC)), "admitted, never handled"


async def _settle(teams: TeamsApp) -> None:
    async with asyncio.timeout(10):
        while teams._tasks:
            await asyncio.gather(*list(teams._tasks))
            await asyncio.sleep(0)  # lets a finished task's done callback drop it


async def _boot(teams: TeamsApp) -> None:
    await teams.start()
    await _settle(teams)


def _texts(sender: FakeSender) -> list[str | None]:
    return [shown_text(a) for a in sender.activities]


async def test_a_drain_tells_the_chat_once_about_queued_messages_even_when_repeated(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    started = asyncio.Event()

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        started.set()
        await asyncio.Event().wait()

    with patch.object(TeamsApp, "_run_turn", _turn):
        teams.spawn(teams._admit(make_inbound("one")), name="turn")
        await started.wait()
        two, three = _sent("two", 2), _sent("three", 3)
        await teams._admit(two)
        await teams._admit(three)
        await teams.drain(timeout=0.0)
        await teams._hold([two, three])  # a second drain pass

    assert _texts(sender) == [HOLDING], "one notice for the chat, however often it drains"
    [held] = sender.activities
    assert held.attachments[0].content.body[0].type == "TextBlock", "a hold is information, not red"


async def test_a_drain_tells_a_chat_once_when_a_message_arrives_behind_a_queued_one(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    started, release = asyncio.Event(), asyncio.Event()

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        started.set()
        await release.wait()

    with patch.object(TeamsApp, "_run_turn", _turn):
        teams.spawn(teams._admit(make_inbound("one")), name="turn")
        await started.wait()
        await teams._admit(_sent("two", 2))
        drain = asyncio.create_task(teams.drain(timeout=5.0))
        await asyncio.sleep(0)
        await teams.handle_message(_message(make_message_activity(text="three", activity_id="a-3")))
        release.set()
        await drain

    assert _texts(sender) == [HOLDING], "the queued message and the arrival share one notice"


async def test_a_turn_ending_during_the_drain_starts_no_queued_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A queued turn started now would be cut off: the next boot runs it instead."""
    teams = _app(db_session_factory, FakeSender())
    started, release = asyncio.Event(), asyncio.Event()
    ran: list[str] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        ran.append(inbound.text)
        started.set()
        await release.wait()

    with patch.object(TeamsApp, "_run_turn", _turn):
        teams.spawn(teams._admit(make_inbound("one")), name="turn")
        await started.wait()
        await teams._admit(make_inbound("two"))
        drain = asyncio.create_task(teams.drain(timeout=5.0))
        await asyncio.sleep(0)
        release.set()
        await drain
        assert ran == ["one"], "nothing queued starts once the drain began"
        await _boot(_app(db_session_factory, FakeSender()))

    assert ran == ["one", "two"], "the next process runs the held message"


async def test_a_message_arriving_during_the_drain_is_held_unless_unaddressed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    teams.draining = True

    await teams.handle_message(_message(make_message_activity(text="later", activity_id="a-2")))
    chatter = make_channel_activity(mention_bot=False, conversation_id=THREAD_ID, activity_id="a-3")
    await teams.handle_message(_message(chatter))
    ran: list[str] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        ran.append(inbound.activity_id)

    with patch.object(TeamsApp, "_run_turn", _turn):
        await _boot(_app(db_session_factory, FakeSender()))

    assert _texts(sender) == [HOLDING], "only the asker is told"
    assert ran == ["a-2"], "the addressed message is replayed; chatter is not held"


def _policy_unread_once() -> AsyncMock:
    """The access policy read fails once (`UNKNOWN`), then reads unprotected."""
    states = [ProtectionState.UNKNOWN]
    return AsyncMock(
        side_effect=lambda *_, **__: states.pop() if states else ProtectionState.UNPROTECTED
    )


async def test_an_unread_access_policy_retries_the_message_instead_of_dropping_it(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)

    with (
        patch.object(app_module, "protection_state", _policy_unread_once()),
        patch.object(TeamsApp, "_run_turn", AsyncMock()) as turn,
    ):
        await teams._admit(_sent("hi", 0, now=True))
        await _settle(teams)
        turn.assert_not_awaited()
        await teams._retry_unfenced()
        await _settle(teams)

    turn.assert_awaited_once()
    assert _texts(sender) == [], "nothing is said while the policy is unread"


async def test_an_arrival_during_the_drain_with_an_unread_policy_is_kept_untold(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    teams.draining = True

    with patch.object(app_module, "protection_state", _policy_unread_once()):
        await teams.handle_message(_message(make_message_activity(activity_id="a-2")))
    with patch.object(TeamsApp, "_run_turn", AsyncMock()) as turn:
        await _boot(_app(db_session_factory, FakeSender()))

    assert _texts(sender) == [], "no notice into a chat whose policy went unread"
    turn.assert_awaited_once()


@pytest.mark.parametrize("draining", [True, False])
async def test_an_unrecorded_arrival_is_not_answered(
    db_session_factory: async_sessionmaker[AsyncSession], draining: bool
) -> None:
    """The write may have landed after all: a notice could precede its own replay."""
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    teams.draining = draining
    down = OperationalError("INSERT", {}, Exception("database is down"))

    with (
        patch.object(TeamsAdmissions, "claim", AsyncMock(side_effect=down)),
        patch.object(TeamsApp, "_run_turn", AsyncMock()) as turn,
    ):
        await teams.handle_message(_message(make_message_activity(activity_id="a-1")))
        await _settle(teams)

    turn.assert_not_awaited()
    assert _texts(sender) == [], "silent: nothing said without a recorded admission"


async def test_a_message_arriving_during_a_slow_boot_sweep_survives_a_drain(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """It is claimed before it waits for the sweep, so the next boot replays it."""
    teams = _app(db_session_factory, FakeSender())
    claimed = asyncio.Event()
    claim = TeamsAdmissions.claim

    async def _claim(self: TeamsAdmissions, inbound: TeamsInbound, *, now: datetime) -> bool:
        try:
            return await claim(self, inbound, now=now)
        finally:
            claimed.set()

    with (
        patch.object(app_module, "retire_orphaned_turns", lambda **_: asyncio.Event().wait()),
        patch.object(TeamsAdmissions, "claim", _claim),
    ):
        teams.start()
        await teams.handle_message(_message(make_message_activity(text="hi", activity_id="a-1")))
        await claimed.wait()
        await teams.drain(timeout=0.0)
    ran: list[str] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        ran.append(inbound.text)

    with patch.object(TeamsApp, "_run_turn", _turn):
        await _boot(_app(db_session_factory, FakeSender()))

    assert ran == ["hi"], "the next process runs it"


async def test_boot_replays_unhandled_messages_once_in_order(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The later message waits behind the first's turn, as a live one would."""
    first, second = _sent("one", 1), _sent("two", 2)
    await _left_by_exited_process(db_session_factory, second, first)
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    started, release = asyncio.Event(), asyncio.Event()
    ran: list[str] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        ran.append(inbound.text)
        started.set()
        await release.wait()

    with patch.object(TeamsApp, "_run_turn", _turn):
        await teams.start()
        async with asyncio.timeout(5):
            await started.wait()
        await asyncio.sleep(0.05)
        assert ran == ["one"], "the second message queues behind the first"
        release.set()
        await _settle(teams)
        await _boot(_app(db_session_factory, FakeSender()))

    assert ran == ["one", "two"], "each message runs once, oldest first, across boots"


@pytest.mark.parametrize("same_process", [True, False])
async def test_a_replayed_message_redelivered_later_runs_once(
    db_session_factory: async_sessionmaker[AsyncSession], same_process: bool
) -> None:
    await _left_by_exited_process(db_session_factory, _sent("hi", 1, activity_id="a-1"))
    teams = _app(db_session_factory, FakeSender())
    ran: list[str] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        ran.append(inbound.activity_id)

    with patch.object(TeamsApp, "_run_turn", _turn):
        await _boot(teams)
        late = teams if same_process else _app(db_session_factory, FakeSender())
        payload = make_message_activity(activity_id="a-1", conversation_id=CONVERSATION_ID)
        await late.handle_message(_message(payload))
        await _settle(late)

    assert ran == ["a-1"], "the redelivery is the same message: no second turn"


async def test_a_message_delivered_to_two_processes_runs_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ran: list[str] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        ran.append(inbound.activity_id)

    payload = make_message_activity(activity_id="a-1")
    with patch.object(TeamsApp, "_run_turn", _turn):
        for teams in (
            _app(db_session_factory, FakeSender()),
            _app(db_session_factory, FakeSender()),
        ):
            await teams.handle_message(_message(payload))
            await teams.handle_message(_message(payload))
            await _settle(teams)

    assert ran == ["a-1"], "one turn, whichever process and however often it arrives"


async def test_a_stale_message_is_not_replayed_and_the_chat_is_told_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _left_by_exited_process(db_session_factory, make_inbound("old"))
    senders = [FakeSender(), FakeSender()]

    with (
        patch.object(admissions_module, "REPLAY_MAX_AGE", timedelta(minutes=-1)),
        patch.object(TeamsApp, "_run_turn", AsyncMock()) as turn,
    ):
        for sender in senders:
            await _boot(_app(db_session_factory, sender))

    turn.assert_not_awaited()
    assert [_texts(s) for s in senders] == [[NOT_HELD], []], "asked to resend, once"


@pytest.mark.parametrize("state", [ProtectionState.PROTECTED, ProtectionState.UNKNOWN])
async def test_a_stale_message_where_daimon_may_not_post_is_closed_untold(
    db_session_factory: async_sessionmaker[AsyncSession], state: ProtectionState
) -> None:
    await _left_by_exited_process(db_session_factory, make_inbound("old"))
    sender = FakeSender()

    with (
        patch.object(admissions_module, "REPLAY_MAX_AGE", timedelta(minutes=-1)),
        patch.object(app_module, "protection_state", AsyncMock(return_value=state)),
    ):
        await _boot(_app(db_session_factory, sender))

    assert _texts(sender) == [], "no resend notice into a protected or unread channel"


async def test_a_retry_older_than_the_replay_window_is_closed_not_cycled(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A policy that stays unread does not keep a message cycling through the retry."""
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    unread = AsyncMock(return_value=ProtectionState.UNKNOWN)

    with (
        patch.object(app_module, "protection_state", unread),
        patch.object(TeamsApp, "_run_turn", AsyncMock()) as turn,
    ):
        await teams._admit(old := _sent("old", 0))
        await _settle(teams)
        assert len(teams._unfenced) == 1, "kept for the retry"
        await teams._retry_unfenced()
        await _settle(teams)

    assert teams._unfenced == [], "closed, not queued again"
    assert await teams._admissions.unhandled([old]) == [], "its row is handled"
    turn.assert_not_awaited()
    assert _texts(sender) == [], "and nothing said where the policy is unread"


async def test_stale_retries_in_two_chats_are_each_closed_and_told_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    first = _sent("a", 0)
    other = dataclasses.replace(
        _sent("b", 1), conversation_id="a:conversation-2", channel_id="a:conversation-2"
    )

    unread = AsyncMock(return_value=ProtectionState.UNKNOWN)
    with patch.object(app_module, "protection_state", unread):
        await teams._admit(first)
        await teams._admit(other)
        await _settle(teams)
    unread.return_value = ProtectionState.UNPROTECTED
    with patch.object(app_module, "protection_state", unread):
        await teams._retry_unfenced()
        await _settle(teams)

    assert sorted(chat for chat, _, _ in sender.sent) == [CONVERSATION_ID, "a:conversation-2"]
    assert _texts(sender) == [NOT_HELD, NOT_HELD], "each chat told once"
    assert await teams._admissions.unhandled([first, other]) == [], "both fenced"


async def test_a_replay_goes_through_admission_like_a_fresh_message(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The tenant went away during the restart: the replay is refused, not run."""
    await _left_by_exited_process(db_session_factory, make_inbound("hi"))
    await set_provision_status(db_session_factory, tenant_id=TENANT, status="failed")
    sender = FakeSender()

    with patch.object(TeamsApp, "_run_turn", AsyncMock()) as turn:
        await _boot(_app(db_session_factory, sender))

    turn.assert_not_awaited()
    assert _texts(sender) == [DENIED], "refused as a fresh message is"


async def test_a_process_that_lost_its_message_to_a_reclaim_neither_runs_nor_answers_it(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The finish fence: the old process resumes after another took its message over."""
    old_sender = FakeSender()
    old = _app(db_session_factory, old_sender)
    inbound = make_inbound("hi")
    assert await old._admissions.claim(inbound, now=datetime.now(UTC))
    ran: list[str] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        ran.append(inbound.activity_id)

    with patch.object(TeamsApp, "_run_turn", _turn):
        await _boot(_app(db_session_factory, FakeSender()))
        await old._intake(inbound)

    assert ran == [inbound.activity_id], "only the reclaiming process runs it"
    assert _texts(old_sender) == [], "and the old one says nothing"


async def test_a_failed_turn_start_answers_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An error notice is fenced too: a redelivery after it is refused, not retried."""
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    payload = make_message_activity(activity_id="a-1")
    down = OperationalError("SELECT", {}, Exception("database is down"))

    with patch.object(app_module, "live_tenant_id", AsyncMock(side_effect=down)):
        await teams.handle_message(_message(payload))
        await _settle(teams)
    with patch.object(TeamsApp, "_run_turn", AsyncMock()) as turn:
        again = _app(db_session_factory, sender)
        await again.handle_message(_message(payload))
        await _settle(again)

    turn.assert_not_awaited()
    assert _texts(sender) == [app_module._FAILED], "one error notice, then nothing"


async def test_an_error_notice_that_cannot_be_fenced_is_not_sent_and_the_message_replays(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sent unfenced, the notice would go out and the message replay after it as well."""
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    inbound = make_inbound("hi")
    down = OperationalError("SELECT", {}, Exception("database is down"))

    with (
        patch.object(app_module, "live_tenant_id", AsyncMock(side_effect=down)),
        patch.object(TeamsAdmissions, "finish", AsyncMock(side_effect=down)),
    ):
        await teams._admit(inbound)
    with patch.object(TeamsApp, "_run_turn", AsyncMock()) as turn:
        await _boot(_app(db_session_factory, FakeSender()))

    assert _texts(sender) == [], "no notice without a finished admission"
    turn.assert_awaited_once()


async def test_a_queued_message_another_process_has_drops_alone_from_the_batch(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    started, release = asyncio.Event(), asyncio.Event()
    ran: list[str] = []
    two, three = _sent("two", 2), _sent("three", 3)
    finish = admissions_module.finish_teams_messages

    async def _finish(session: AsyncSession, **kwargs: Any) -> set[str]:
        return await finish(session, **kwargs) - {two.activity_id}

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        ran.append(inbound.text)
        started.set()
        await release.wait()

    with (
        patch.object(TeamsApp, "_run_turn", _turn),
        patch.object(admissions_module, "finish_teams_messages", _finish),
    ):
        teams.spawn(teams._admit(make_inbound("one")), name="turn")
        await started.wait()
        await teams._admit(two)
        await teams._admit(three)
        release.set()
        await _settle(teams)

    assert ran == ["one", "three"], "the batch's turn runs without the other process's message"
    assert _texts(sender) == [], "and nothing is said about it"


async def test_a_batch_whose_fence_fails_stays_silent_and_runs_on_the_retry(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A database error leaves the whole batch unhandled; the reclaim loop runs it again."""
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    started, release = asyncio.Event(), asyncio.Event()
    ran: list[str] = []
    two, three = _sent("two", 2), _sent("three", 3)
    finish = TeamsAdmissions.finish

    async def _finish(self: TeamsAdmissions, items: list[TeamsInbound]) -> list[TeamsInbound]:
        if two in items:
            raise OperationalError("UPDATE", {}, Exception("database is down"))
        return await finish(self, items)

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        ran.append(inbound.text)
        started.set()
        await release.wait()

    with (
        patch.object(TeamsApp, "_run_turn", _turn),
        patch.object(admissions_module, "REPLAY_MAX_AGE", timedelta(days=3650)),
    ):
        with patch.object(TeamsAdmissions, "finish", _finish):
            teams.spawn(teams._admit(make_inbound("one")), name="turn")
            await started.wait()
            await teams._admit(two)
            await teams._admit(three)
            release.set()
            await _settle(teams)
        assert ran == ["one"], "no part of the batch runs unfenced"
        await teams._retry_unfenced()
        await _settle(teams)
        await teams._retry_unfenced()
        await _settle(teams)

    assert ran == ["one", "two", "three"], "the retry runs each held message once, in order"
    assert _texts(sender) == [], "and nothing was said while it waited"


async def test_the_retry_never_reruns_a_started_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A failed fence after the turn (its error notice's) leaves a done message done."""
    teams = _app(db_session_factory, FakeSender())
    inbound = make_inbound("hi")

    with patch.object(TeamsApp, "_run_turn", AsyncMock()) as turn:
        await teams._admit(inbound)
        teams._unfenced.append(inbound)
        await teams._retry_unfenced()
        await _settle(teams)

    turn.assert_awaited_once()


async def test_a_typed_name_thread_reply_is_kept_for_a_replay(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reply = dataclasses.replace(
        make_inbound("@daimon look", kind="channel"), unprompted=True, by_name=True
    )
    await _left_by_exited_process(db_session_factory, reply)
    gate = TeamsAdmissions(db_session_factory, TENANT, owner_key=uuid.uuid4().int >> 66)

    reclaimed = await gate.reclaim(now=datetime.now(UTC))

    assert reclaimed.replay == [reply], "it may start a turn, so a restart must not lose it"


@pytest.mark.parametrize("switch_on", [True, False])
async def test_a_stale_typed_name_reply_is_told_only_while_its_switch_is_on(
    db_session_factory: async_sessionmaker[AsyncSession], switch_on: bool
) -> None:
    reply = dataclasses.replace(
        make_inbound("@daimon look", kind="channel"), unprompted=True, by_name=True
    )
    await _left_by_exited_process(db_session_factory, reply)
    sender = FakeSender()

    with (
        patch.object(admissions_module, "REPLAY_MAX_AGE", timedelta(minutes=-1)),
        patch.object(TeamsApp, "_plain_text_mentions_on", AsyncMock(return_value=switch_on)),
    ):
        await _boot(_app(db_session_factory, sender))

    assert _texts(sender) == ([NOT_HELD] if switch_on else []), "told only if it still asks"


async def test_a_message_reaching_its_turn_during_the_drain_is_held_not_started(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Admitted before the drain, routed after: a turn started now would be cut off."""
    sender = FakeSender()
    teams = _app(db_session_factory, sender)
    inbound = make_inbound("hi")
    assert await teams._admissions.claim(inbound, now=datetime.now(UTC))
    teams.draining = True
    ran: list[str] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        ran.append(inbound.text)

    with patch.object(TeamsApp, "_run_turn", _turn):
        await teams._intake(inbound)
        assert ran == [], "no turn starts once the drain began"
        await _boot(_app(db_session_factory, FakeSender()))

    assert ran == ["hi"], "the next process runs it"
    assert _texts(sender) == [HOLDING], "and the chat is told once it is held"
