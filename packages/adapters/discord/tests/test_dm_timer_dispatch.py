"""Imported timers fire: DM, thread, channel and unusable scopes through the wake poller.

`poll_wakes_once` -> `DaimonBot._open_wake_thread` is the production path; every
row ends settled or delivered, none is deferred forever. `deliver_dm_timer` is
tested on its own for the delivery rules.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.dm_timer_delivery import deliver_dm_timer
from daimon.core.config import DirectMessagePolicy
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.wakes import enqueue_wake, poll_wakes_once
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.stores.domain import ContinuationReason, TaskContinuationRow
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.turn.errors import AdmissionDenied
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import make_bot
from .test_continuation_dispatch_entry import _make_runtime  # pyright: ignore[reportPrivateUsage]

_GUILD = "720000001"
_OWNER = "555"


async def _seed(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    thread_id: str,
    reason: ContinuationReason = "timer",
    workspace_id: str = _GUILD,
    dm: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=workspace_id)
        if dm:
            await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
    request = ContinuationRequest(
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="dm",
        thread_id=thread_id,
        requester_account_id=uuid.uuid4(),
        requester_external_user_id=_OWNER,
        target_ma_agent_id="ag_target",
        target_name="target-agent",
        requested_work="drink some water",
        reason=reason,
        idempotency_key=uuid.uuid4(),
    )
    await enqueue_wake(sessionmaker, request, available_at=datetime.now(UTC) - timedelta(seconds=1))
    return tenant.id, request.idempotency_key


async def _row(
    sessionmaker: async_sessionmaker[AsyncSession], key: uuid.UUID
) -> TaskContinuationRow:
    async with sessionmaker() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None
    return row


async def _poll(sessionmaker: async_sessionmaker[AsyncSession], bot: DaimonBot) -> None:
    await poll_wakes_once(
        sessionmaker,
        platform="discord",
        open_thread=bot._open_wake_thread,  # pyright: ignore[reportPrivateUsage]
        now=datetime.now(UTC),
    )
    for task in list(bot._bg_tasks):  # pyright: ignore[reportPrivateUsage]
        await task


def _bot(sessionmaker: async_sessionmaker[AsyncSession]) -> DaimonBot:
    return make_bot(_make_runtime(sessionmaker))


async def test_a_dm_timer_runs_in_the_owners_dm_and_is_delivered(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, key = await _seed(db_session_factory, thread_id=f"dm:{uuid.uuid4()}")
    bot = _bot(db_session_factory)
    run = AsyncMock()
    bot._run_dm_timer = run  # pyright: ignore[reportPrivateUsage]
    bot.get_channel = MagicMock()  # pyright: ignore[reportAttributeAccessIssue]
    bot.fetch_channel = AsyncMock()  # pyright: ignore[reportAttributeAccessIssue]

    await _poll(db_session_factory, bot)

    run.assert_awaited_once()
    assert run.await_args is not None
    seed = run.await_args.args[1]
    assert "drink some water" in seed
    bot.get_channel.assert_not_called()
    bot.fetch_channel.assert_not_awaited()
    assert (await _row(db_session_factory, key)).status == "delivered"
    await _poll(db_session_factory, bot)
    run.assert_awaited_once()


async def test_a_dm_timer_the_owner_may_no_longer_receive_is_skipped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, key = await _seed(db_session_factory, thread_id=f"dm:{uuid.uuid4()}")
    bot = _bot(db_session_factory)
    bot._run_dm_timer = AsyncMock(  # pyright: ignore[reportPrivateUsage]
        side_effect=AdmissionDenied(reason="invoker_not_allowed")
    )

    await _poll(db_session_factory, bot)

    row = await _row(db_session_factory, key)
    assert row.status == "skipped"
    assert row.skip_reason == "admission_denied:invoker_not_allowed"


async def test_a_non_timer_in_a_dm_scope_is_skipped_not_run(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, key = await _seed(db_session_factory, thread_id=f"dm:{uuid.uuid4()}", reason="task_handoff")
    bot = _bot(db_session_factory)
    run = AsyncMock()
    bot._run_dm_timer = run  # pyright: ignore[reportPrivateUsage]

    await _poll(db_session_factory, bot)

    run.assert_not_awaited()
    row = await _row(db_session_factory, key)
    assert (row.status, row.skip_reason) == ("skipped", "thread_unavailable")


async def test_a_thread_timer_resumes_its_thread(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, key = await _seed(db_session_factory, thread_id="4242")
    bot = _bot(db_session_factory)
    thread = MagicMock(spec=discord.Thread)
    thread.id = 4242
    thread.guild = MagicMock()
    thread.guild.id = int(_GUILD)
    bot.get_channel = MagicMock(return_value=thread)  # pyright: ignore[reportAttributeAccessIssue]
    dispatched = AsyncMock()
    bot.dispatch_continuations_in_thread = dispatched  # pyright: ignore[reportAttributeAccessIssue]

    await _poll(db_session_factory, bot)

    dispatched.assert_awaited_once_with(tenant_id=tenant_id, thread=thread, guild_id=_GUILD)
    assert (await _row(db_session_factory, key)).status == "pending"


async def test_a_timer_naming_a_channel_is_skipped_not_deferred(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, key = await _seed(db_session_factory, thread_id="4243")
    bot = _bot(db_session_factory)
    bot.get_channel = MagicMock(  # pyright: ignore[reportAttributeAccessIssue]
        return_value=MagicMock(spec=discord.TextChannel)
    )

    await _poll(db_session_factory, bot)

    row = await _row(db_session_factory, key)
    assert (row.status, row.skip_reason) == ("skipped", "thread_unavailable")


@pytest.mark.parametrize("scope", ["not-a-number", "channel:12"])
async def test_a_scope_that_is_no_channel_is_skipped_not_deferred(
    db_session_factory: async_sessionmaker[AsyncSession], scope: str
) -> None:
    _, key = await _seed(db_session_factory, thread_id=scope)
    bot = _bot(db_session_factory)
    bot.get_channel = MagicMock()  # pyright: ignore[reportAttributeAccessIssue]

    await _poll(db_session_factory, bot)

    row = await _row(db_session_factory, key)
    assert (row.status, row.skip_reason) == ("skipped", "thread_unavailable")
    bot.get_channel.assert_not_called()


# deliver_dm_timer ----------------------------------------------------------------


@dataclass
class _Delivery:
    row: TaskContinuationRow
    run_turn: AsyncMock
    channel: MagicMock
    policy: DirectMessagePolicy
    present: bool

    async def run(self, sessionmaker: async_sessionmaker[AsyncSession]) -> int:
        policy = self.policy
        return await deliver_dm_timer(
            sessionmaker,
            self.row,
            run_turn=self.run_turn,
            open_dm=AsyncMock(return_value=self.channel),
            member_present=AsyncMock(return_value=self.present),
            dm_policy=lambda _tenant: policy,
        )


async def _delivery(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    dm: bool = True,
    policy: DirectMessagePolicy | None = None,
    present: bool = True,
    text: str = "hello",
    forbidden: bool = False,
) -> _Delivery:
    _, key = await _seed(
        sessionmaker, thread_id=f"dm:{uuid.uuid4()}", workspace_id="720000099", dm=dm
    )
    channel = MagicMock()
    channel.send = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403, reason="x"), "closed")
        if forbidden
        else None
    )
    return _Delivery(
        row=await _row(sessionmaker, key),
        run_turn=AsyncMock(return_value=text),
        channel=channel,
        policy=policy or DirectMessagePolicy(),
        present=present,
    )


async def test_the_reply_is_sent_in_chunks_with_mentions_off(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    delivery = await _delivery(db_session_factory, text="word " * 900)

    sent = await delivery.run(db_session_factory)

    assert sent == 3
    assert delivery.channel.send.await_count == 3
    for call in delivery.channel.send.await_args_list:
        assert len(call.kwargs["content"]) <= 2000
        assert call.kwargs["allowed_mentions"].everyone is False
        assert call.kwargs["allowed_mentions"].users is False


@pytest.mark.parametrize("case", ["dm_off", "policy_disabled", "not_allowlisted", "left_guild"])
async def test_no_turn_runs_when_the_owner_cannot_receive_dms(
    db_session_factory: async_sessionmaker[AsyncSession], case: str
) -> None:
    policies = {
        "policy_disabled": DirectMessagePolicy(mode="disabled"),
        "not_allowlisted": DirectMessagePolicy(mode="allowlist", recipient_ids=["999"]),
    }
    delivery = await _delivery(
        db_session_factory,
        dm=case != "dm_off",
        policy=policies.get(case),
        present=case != "left_guild",
    )

    with pytest.raises(AdmissionDenied):
        await delivery.run(db_session_factory)

    delivery.run_turn.assert_not_awaited()
    delivery.channel.send.assert_not_awaited()


async def test_a_closed_dm_settles_as_not_allowed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    delivery = await _delivery(db_session_factory, forbidden=True)

    with pytest.raises(AdmissionDenied) as caught:
        await delivery.run(db_session_factory)

    assert caught.value.reason == "invoker_not_allowed"
