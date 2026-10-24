"""Tests for `continuation_dispatch.dispatch_pending_continuations`.

Real Postgres for the `task_continuations` rows (the claim-once guarantee is
a real conditional UPDATE); `run_follow_up` is a plain injected async
callable, per the module's own DI-friendly design, so these tests assert
dispatch happened without paying for a second real turn.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from daimon.adapters.discord.continuation_dispatch import (
    _HISTORY_MAX_PAGES,
    _HISTORY_PAGE_SIZE,
    _latest_human_message_at,
    dispatch_pending_continuations,
)
from daimon.core.continuity.continuation import ContinuationDecision
from daimon.core.continuity.dispatch import ContinuationContextUnavailable
from daimon.core.continuity.wakes import (
    WAKE_CLAIM_LEASE,
    WAKE_RETRY_DELAY,
    WAKE_RUN_LEASE,
    abandon_interrupted_wakes,
    list_due_wake_threads,
)
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.task_continuations import (
    get_continuation,
    list_pending_continuations,
    record_continuation,
)
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TENANT_UUID = uuid.uuid4()


class _SimulatedProcessDeath(BaseException):
    """Bypass dispatcher error handling to model abrupt process termination."""


class _AsyncIter:
    def __init__(self, items: list[object]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> object:
        try:
            return next(self._items)
        except StopIteration as err:
            raise StopAsyncIteration from err


def _make_thread(*, thread_id: int = 42) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.history = MagicMock(return_value=_AsyncIter([]))
    thread.send = AsyncMock()
    return thread


async def test_superseding_human_message_found_behind_six_bot_posts() -> None:
    """A busy bot must not hide a human reply that superseded a queued handoff."""
    created = datetime.now(UTC) - timedelta(minutes=1)
    human_at = created + timedelta(seconds=1)
    posts = [
        SimpleNamespace(
            author=SimpleNamespace(bot=True), created_at=human_at + timedelta(seconds=i)
        )
        for i in range(6, 0, -1)
    ]
    posts.append(SimpleNamespace(author=SimpleNamespace(bot=False), created_at=human_at))
    thread = _make_thread()
    thread.history.side_effect = lambda *, limit, before=None: _AsyncIter(posts[:limit])

    assert await _latest_human_message_at(thread, after=created) == human_at


async def test_superseding_human_message_found_on_second_history_page() -> None:
    created = datetime.now(UTC) - timedelta(minutes=10)
    human_at = created + timedelta(seconds=1)
    posts = [
        SimpleNamespace(
            author=SimpleNamespace(bot=i != 100),
            created_at=human_at + timedelta(seconds=101 - i),
        )
        for i in range(101)
    ]
    thread = _make_thread()

    def history(*, limit: int, before: object = None) -> _AsyncIter:
        start = 0 if before is None else posts.index(before) + 1
        return _AsyncIter(posts[start : start + limit])

    thread.history.side_effect = history
    assert await _latest_human_message_at(thread, after=created) == posts[-1].created_at
    assert thread.history.call_count == 2


async def test_history_scan_stops_at_continuation_timestamp() -> None:
    created = datetime.now(UTC) - timedelta(minutes=1)
    posts = [
        SimpleNamespace(
            author=SimpleNamespace(bot=True), created_at=created + timedelta(seconds=1)
        ),
        SimpleNamespace(author=SimpleNamespace(bot=False), created_at=created),
    ]
    thread = _make_thread()
    thread.history.side_effect = lambda *, limit, before=None: _AsyncIter(posts)

    assert await _latest_human_message_at(thread, after=created) is None
    assert thread.history.call_count == 1


async def test_history_scan_cap_counts_as_superseded() -> None:
    created = datetime.now(UTC) - timedelta(days=1)
    thread = _make_thread()
    calls = 0

    def history(*, limit: int, before: object = None) -> _AsyncIter:
        nonlocal calls
        calls += 1
        posts = [
            SimpleNamespace(
                author=SimpleNamespace(bot=True),
                created_at=created + timedelta(hours=1, seconds=-(calls * limit + i)),
            )
            for i in range(limit)
        ]
        return _AsyncIter(posts)

    thread.history.side_effect = history
    # Retrying would rescan the same pages, so the cap resolves the row instead:
    # a time after the continuation, which decide_continuation reads as superseded.
    latest = await _latest_human_message_at(thread, after=created)
    assert latest is not None and latest > created
    assert calls == _HISTORY_MAX_PAGES
    assert thread.history.call_args.kwargs["limit"] == _HISTORY_PAGE_SIZE


async def test_failed_history_read_is_unknown() -> None:
    thread = _make_thread()
    thread.history.side_effect = discord.HTTPException(
        SimpleNamespace(status=503, reason="Unavailable"), "history unavailable"
    )
    with pytest.raises(ContinuationContextUnavailable):
        await _latest_human_message_at(thread, after=datetime.now(UTC) - timedelta(minutes=1))


async def _seed_pending_row(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    requested_work: str | None,
    available_at: datetime | None = None,
    reason: str = "task_handoff",
) -> tuple[uuid.UUID, uuid.UUID, str, uuid.UUID]:
    """Seed one pending continuation row; return (tenant_id, account_id, thread_id, key)."""
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(
            session, platform="discord", workspace_id=str(uuid.uuid4().int)[:9]
        )
        account = await make_account(session, tenant=tenant)
        idempotency_key = uuid.uuid4()
        await record_continuation(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="parent-1",
            thread_id="42",
            requester_account_id=account.id,
            requester_external_user_id="555",
            target_ma_agent_id="ag_target",
            target_name="target-agent",
            reason=reason,
            idempotency_key=idempotency_key,
            requested_work=requested_work,
            available_at=available_at,
        )
    return tenant.id, account.id, "42", idempotency_key


async def _may_post_open() -> bool:
    """The thread is one the agent may post in (no access policy)."""
    return True


async def test_dispatches_exactly_once_and_settles_delivered(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _account_id, _thread_id, key = await _seed_pending_row(
        db_session_factory, requested_work="pick up the report"
    )
    thread = _make_thread()
    anthropic = build_stub_anthropic(_reachable_agent_handler(tenant_id, ma_agent_id="ag_target"))
    run_follow_up = AsyncMock()

    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=run_follow_up,
        may_post=_may_post_open,
    )

    run_follow_up.assert_awaited_once()
    assert run_follow_up.await_args is not None
    row_arg, decision_arg = run_follow_up.await_args.args
    assert isinstance(row_arg, TaskContinuationRow)
    assert isinstance(decision_arg, ContinuationDecision)
    assert decision_arg.seed_user_message == "pick up the report"

    async with db_session_factory() as session:
        settled = await get_continuation(session, idempotency_key=key)
    assert settled is not None
    assert settled.status == "delivered"

    # A second call against the same thread must not re-dispatch: the row
    # is no longer pending.
    run_follow_up.reset_mock()
    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=run_follow_up,
        may_post=_may_post_open,
    )
    run_follow_up.assert_not_awaited()


async def test_unreadable_history_defers_handoff(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _account_id, _thread_id, key = await _seed_pending_row(
        db_session_factory, requested_work="continue"
    )
    thread = _make_thread()
    thread.history.side_effect = discord.HTTPException(
        SimpleNamespace(status=503, reason="Unavailable"), "history unavailable"
    )
    run_follow_up = AsyncMock()

    await dispatch_pending_continuations(
        db_session_factory,
        MagicMock(),
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=run_follow_up,
        may_post=_may_post_open,
    )

    run_follow_up.assert_not_awaited()
    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None and row.status == "pending"
    assert row.available_at is not None


async def test_history_cap_settles_handoff_instead_of_retrying_forever(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _account_id, _thread_id, key = await _seed_pending_row(
        db_session_factory, requested_work="continue"
    )
    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None
    newest = row.created_at + timedelta(days=1)
    thread = _make_thread()
    thread.history.side_effect = lambda *, limit, before=None: _AsyncIter(
        [SimpleNamespace(author=SimpleNamespace(bot=True), created_at=newest) for _ in range(limit)]
    )
    run_follow_up = AsyncMock()
    anthropic = build_stub_anthropic(_reachable_agent_handler(tenant_id, ma_agent_id="ag_target"))

    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=run_follow_up,
        may_post=_may_post_open,
    )

    run_follow_up.assert_not_awaited()
    async with db_session_factory() as session:
        settled = await get_continuation(session, idempotency_key=key)
    assert settled is not None and settled.status == "skipped"
    assert settled.skip_reason == "skip_superseded"


async def test_timer_dispatches_without_reading_thread_history(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _account_id, _thread_id, key = await _seed_pending_row(
        db_session_factory,
        requested_work="check the build",
        available_at=datetime.now(UTC) - timedelta(seconds=1),
        reason="timer",
    )
    thread = _make_thread()
    thread.history.side_effect = discord.HTTPException(
        SimpleNamespace(status=503, reason="Unavailable"), "history unavailable"
    )
    run_follow_up = AsyncMock()
    anthropic = build_stub_anthropic(_reachable_agent_handler(tenant_id, ma_agent_id="ag_target"))

    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=run_follow_up,
        may_post=_may_post_open,
    )

    thread.history.assert_not_called()
    run_follow_up.assert_awaited_once()


async def test_newer_human_behind_bot_posts_skips_handoff(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _account_id, _thread_id, key = await _seed_pending_row(
        db_session_factory, requested_work="continue"
    )
    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None
    human_at = row.created_at + timedelta(seconds=1)
    posts = [
        SimpleNamespace(
            author=SimpleNamespace(bot=True), created_at=human_at + timedelta(seconds=i)
        )
        for i in range(6, 0, -1)
    ]
    posts.append(SimpleNamespace(id=1001, author=SimpleNamespace(bot=False), created_at=human_at))
    thread = _make_thread()
    thread.history.side_effect = lambda *, limit, before=None: _AsyncIter(posts[:limit])
    run_follow_up = AsyncMock()
    anthropic = build_stub_anthropic(_reachable_agent_handler(tenant_id, ma_agent_id="ag_target"))

    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=run_follow_up,
        may_post=_may_post_open,
    )

    run_follow_up.assert_not_awaited()
    async with db_session_factory() as session:
        settled = await get_continuation(session, idempotency_key=key)
    assert settled is not None and settled.status == "skipped"
    assert settled.skip_reason == "skip_superseded"


async def test_skips_save_only_continuation_without_dispatch(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _account_id, _thread_id, key = await _seed_pending_row(
        db_session_factory, requested_work=None
    )
    thread = _make_thread()
    run_follow_up = AsyncMock()

    await dispatch_pending_continuations(
        db_session_factory,
        MagicMock(),
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=run_follow_up,
        may_post=_may_post_open,
    )

    run_follow_up.assert_not_awaited()
    thread.send.assert_not_called()  # skip_save_only is a silent skip -- nothing was promised
    async with db_session_factory() as session:
        settled = await get_continuation(session, idempotency_key=key)
    assert settled is not None
    assert settled.status == "skipped"
    assert settled.skip_reason == "skip_save_only"


async def test_preparation_failure_settles_skipped_not_delivered(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.turn.errors import SessionPreparationFailed

    tenant_id, _account_id, _thread_id, key = await _seed_pending_row(
        db_session_factory, requested_work="continue"
    )
    thread = _make_thread()
    anthropic = build_stub_anthropic(_reachable_agent_handler(tenant_id, ma_agent_id="ag_target"))

    async def _failing_follow_up(row: object, decision: object) -> None:
        raise SessionPreparationFailed(
            reasons=("agent_identity",), stage="checkpointed", retry_after=datetime.now(UTC)
        )

    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=_failing_follow_up,
        may_post=_may_post_open,
    )

    async with db_session_factory() as session:
        settled = await get_continuation(session, idempotency_key=key)
    assert settled is not None
    assert settled.status == "skipped"
    assert settled.skip_reason == "blocked_preparation_failed"


async def test_a_handoff_survives_busy_binds_and_the_poller_delivers_it(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Five busy binds, then a free session, delivers the handoff once, from the poller.

    Each busy bind before the send releases the same row with its claim refunded
    and a poll time (#1549 review 1): with `available_at` left NULL it waited for
    a turn tail in the thread, which may never come once the copy owns it.
    """
    from daimon.core.turn.errors import SessionBusyError

    tenant_id, _account_id, thread_id, key = await _seed_pending_row(
        db_session_factory, requested_work="continue"
    )
    thread = _make_thread()
    anthropic = build_stub_anthropic(_reachable_agent_handler(tenant_id, ma_agent_id="ag_target"))
    now = datetime.now(UTC)
    tails = 0
    delivered: list[str] = []

    async def _busy_five_times(row: TaskContinuationRow, decision: ContinuationDecision) -> None:
        nonlocal tails
        tails += 1
        if tails <= 5:
            raise SessionBusyError(pending_reasons=("agent_identity",), retry_after=now)
        delivered.append(decision.seed_user_message or "")

    for tail in range(6):
        await dispatch_pending_continuations(
            db_session_factory,
            anthropic,
            tenant_id=tenant_id,
            thread=thread,
            run_follow_up=_busy_five_times,
            may_post=_may_post_open,
            now=lambda tail=tail: now + timedelta(minutes=tail),
        )
        async with db_session_factory() as session:
            row = await get_continuation(session, idempotency_key=key)
        assert row is not None
        if tail < 5:
            assert row.status == "pending" and row.attempts == 0, "a busy wait is refunded"
            retry_at = now + timedelta(minutes=tail) + WAKE_RETRY_DELAY
            assert row.available_at == retry_at, "the poller retries it"
            assert row.started_at is None and row.lease_owner is None
            due = await list_due_wake_threads(db_session_factory, platform="discord", now=retry_at)
            assert [t.thread_id for t in due if t.tenant_id == tenant_id] == [thread_id]

    assert delivered == ["continue"], "delivered exactly once"
    assert row.status == "delivered" and row.attempts == 1
    async with db_session_factory() as session:
        pending = await list_pending_continuations(
            session, tenant_id=tenant_id, platform="discord", thread_id=thread_id
        )
    assert pending == [], "no second row is ever queued"


async def test_a_busy_wake_is_released_for_the_poller_to_retry(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.turn.errors import SessionBusyError

    tenant_id, _account_id, _thread_id, key = await _seed_pending_row(
        db_session_factory, requested_work="continue", available_at=datetime.now(UTC)
    )
    thread = _make_thread()
    anthropic = build_stub_anthropic(_reachable_agent_handler(tenant_id, ma_agent_id="ag_target"))
    now = datetime.now(UTC) + timedelta(seconds=1)

    async def _busy(row: object, decision: object) -> None:
        raise SessionBusyError(pending_reasons=("agent_identity",), retry_after=now)

    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=_busy,
        may_post=_may_post_open,
        now=lambda: now,
    )
    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None and row.status == "pending" and row.attempts == 0
    assert row.available_at == now + WAKE_RETRY_DELAY


async def test_each_row_is_stamped_with_the_time_it_actually_ran(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second row dispatched after a long first turn gets a fresh lease and fence.

    With one `now` for the whole pass, the second claim was written with a
    lease that had already run out during the first turn.
    """
    tenant_id, account_id, _thread_id, first_key = await _seed_pending_row(
        db_session_factory, requested_work="first job"
    )
    second_key = uuid.uuid4()
    async with db_session_factory() as session, session.begin():
        await record_continuation(
            session,
            tenant_id=tenant_id,
            platform="discord",
            parent_channel_id="parent-1",
            thread_id="42",
            requester_account_id=account_id,
            requester_external_user_id="555",
            target_ma_agent_id="ag_target",
            target_name="target-agent",
            reason="task_handoff",
            idempotency_key=second_key,
            requested_work="second job",
        )
    thread = _make_thread()
    anthropic = build_stub_anthropic(_reachable_agent_handler(tenant_id, ma_agent_id="ag_target"))
    start = datetime.now(UTC)
    clock = [start]
    turn_length = WAKE_CLAIM_LEASE + timedelta(minutes=5)

    async def _slow_first_turn(row: TaskContinuationRow, decision: ContinuationDecision) -> None:
        if row.idempotency_key == first_key:
            clock[0] = clock[0] + turn_length

    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=_slow_first_turn,
        may_post=_may_post_open,
        now=lambda: clock[0],
    )

    async with db_session_factory() as session:
        second = await get_continuation(session, idempotency_key=second_key)
    assert second is not None and second.status == "delivered"
    assert second.claimed_at == start + turn_length, "claimed after the first turn ended"
    assert second.started_at == start + turn_length, "fenced after the first turn ended"
    assert second.delivered_at == start + turn_length


async def test_process_death_before_the_fence_is_retried_after_lease_expiry(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A claim whose holder died before starting the turn runs once, after its lease."""
    tenant_id, _account_id, _thread_id, key = await _seed_pending_row(
        db_session_factory, requested_work="continue"
    )
    anthropic = build_stub_anthropic(_reachable_agent_handler(tenant_id, ma_agent_id="ag_target"))
    now = datetime.now(UTC)
    dying_thread = _make_thread()
    dying_thread.history = MagicMock(side_effect=_SimulatedProcessDeath)
    run_follow_up = AsyncMock()

    with pytest.raises(_SimulatedProcessDeath):
        await dispatch_pending_continuations(
            db_session_factory,
            anthropic,
            tenant_id=tenant_id,
            thread=dying_thread,
            run_follow_up=run_follow_up,
            may_post=_may_post_open,
            now=lambda: now,
        )
    async with db_session_factory() as session:
        stranded = await get_continuation(session, idempotency_key=key)
    assert stranded is not None and stranded.status == "claimed"
    assert stranded.started_at is None

    thread = _make_thread()
    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=run_follow_up,
        may_post=_may_post_open,
        now=lambda: now + timedelta(seconds=1),
    )
    run_follow_up.assert_not_awaited()  # the dead process's lease is still live

    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=run_follow_up,
        may_post=_may_post_open,
        now=lambda: now + WAKE_CLAIM_LEASE + timedelta(seconds=1),
    )
    run_follow_up.assert_awaited_once()
    async with db_session_factory() as session:
        delivered = await get_continuation(session, idempotency_key=key)
    assert delivered is not None and delivered.status == "delivered"
    assert delivered.attempts == 2


@pytest.mark.parametrize(
    "effect_before_crash", [False, True], ids=["before-effect", "after-effect"]
)
async def test_process_death_after_the_fence_settles_interrupted_and_never_reruns(
    db_session_factory: async_sessionmaker[AsyncSession],
    effect_before_crash: bool,
) -> None:
    """A started claim is never run again, whichever side of its effect the process died on.

    The fence cannot tell the two apart, so neither is retried: the lease
    expiry settles the row `skipped/interrupted` instead of stranding it.
    """
    tenant_id, _account_id, _thread_id, key = await _seed_pending_row(
        db_session_factory, requested_work="continue"
    )
    thread = _make_thread()
    anthropic = build_stub_anthropic(_reachable_agent_handler(tenant_id, ma_agent_id="ag_target"))
    visible_effects: list[str] = []
    now = datetime.now(UTC)

    async def _die_during_follow_up(
        row: TaskContinuationRow, decision: ContinuationDecision
    ) -> None:
        if effect_before_crash:
            visible_effects.append(decision.seed_user_message or "")
        raise _SimulatedProcessDeath

    with pytest.raises(_SimulatedProcessDeath):
        await dispatch_pending_continuations(
            db_session_factory,
            anthropic,
            tenant_id=tenant_id,
            thread=thread,
            run_follow_up=_die_during_follow_up,
            may_post=_may_post_open,
            now=lambda: now,
        )

    run_follow_up = AsyncMock()
    after_lease = now + WAKE_RUN_LEASE + timedelta(seconds=1)
    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=run_follow_up,
        may_post=_may_post_open,
        now=lambda: after_lease,
    )
    assert run_follow_up.await_count == 0, "a started claim must never be retried"

    abandoned = await abandon_interrupted_wakes(
        db_session_factory, platform="discord", now=after_lease
    )
    assert [row.idempotency_key for row in abandoned] == [key]
    async with db_session_factory() as session:
        settled = await get_continuation(session, idempotency_key=key)
    assert settled is not None
    assert settled.status == "skipped" and settled.skip_reason == "interrupted"
    assert len(visible_effects) == int(effect_before_crash), (
        "a later dispatch must not repeat the observable effect"
    )


def _reachable_agent_handler(
    tenant_id: uuid.UUID, *, ma_agent_id: str
) -> Callable[[httpx.Request], httpx.Response]:
    """`GET /v1/agents/{id}` handler for a real, tenant-owned, non-archived agent."""
    agent = ma_agent(
        id=ma_agent_id,
        name="target-agent",
        tenant_id=tenant_id,
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=agent.model_dump(mode="json"))

    return _handler


@pytest.mark.parametrize("path", ["responder_changed", "turn_running"])
@pytest.mark.parametrize("may_post", [False, True], ids=["protected-or-unknown", "open"])
async def test_skip_copy_is_posted_only_where_the_agent_may_post(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    may_post: bool,
) -> None:
    """SYS-048: the dispatcher asks the bot's may-post decision before each
    post. A protected or unknown thread gets nothing; the row settles skipped
    either way. `open` is the control."""
    from types import SimpleNamespace

    from daimon.adapters.discord import continuation_dispatch
    from daimon.core.continuity.continuation import ResponderChanged

    tenant_id, _account_id, _thread_id, key = await _seed_pending_row(
        db_session_factory, requested_work="continue"
    )
    thread = _make_thread()
    anthropic = build_stub_anthropic(_reachable_agent_handler(tenant_id, ma_agent_id="ag_target"))
    if path == "turn_running":

        async def _live_turn(*_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(active_turn_message_id="m-1")

        monkeypatch.setattr(continuation_dispatch, "get_live_thread_session", _live_turn)

    async def _rerouted(row: object, decision: object) -> None:
        raise ResponderChanged(target_name="target-agent", current_name="other-agent")

    async def _may_post() -> bool:
        return may_post

    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=_rerouted,
        may_post=_may_post,
    )

    async with db_session_factory() as session:
        settled = await get_continuation(session, idempotency_key=key)
    if path == "turn_running":
        # J6g: a hand-off behind its own running turn waits and runs after it; nobody
        # is told to send another message.
        assert settled is not None and settled.status == "pending"
        assert settled.available_at is not None
        thread.send.assert_not_called()
        return
    assert settled is not None and settled.status == "skipped"
    if may_post:
        thread.send.assert_awaited_once()
    else:
        thread.send.assert_not_called()


async def test_queued_messages_and_the_handoff_each_get_one_answer_in_order(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#1549 review 6: the person wrote twice while the hand-off's turn still held the
    session; both messages were queued behind it. A queued message will be answered, so
    it doesn't make older queued work stale: the hand-off's own work and each message
    get one answer, oldest first, and none is silently dropped. A message the person
    sent instead of waiting (no row of its own) still supersedes."""
    from daimon.core.continuity.continuation import queued_message_key

    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(
            session, platform="discord", workspace_id=str(uuid.uuid4().int)[:9]
        )
        account = await make_account(session, tenant=tenant)
    keys = [uuid.uuid4(), queued_message_key("discord", "11"), queued_message_key("discord", "12")]
    works = ["finish the report", "which model are you?", "and how long will it take?"]
    created: list[datetime] = []
    for key, work in zip(keys, works, strict=True):
        async with db_session_factory() as session, session.begin():
            row = await record_continuation(
                session,
                tenant_id=tenant.id,
                platform="discord",
                parent_channel_id="parent-1",
                thread_id="42",
                requester_account_id=account.id,
                requester_external_user_id="555",
                target_ma_agent_id="ag_target",
                target_name="target-agent",
                reason="task_handoff",
                idempotency_key=key,
                requested_work=work,
                available_at=None if key == keys[0] else datetime.now(UTC),
            )
        created.append(row.created_at)

    def _human(message_id: int, at: datetime) -> SimpleNamespace:
        return SimpleNamespace(id=message_id, author=SimpleNamespace(bot=False), created_at=at)

    # Each queued message was posted just before its own row was written.
    posts = [
        _human(12, created[1] + (created[2] - created[1]) / 2),
        _human(11, created[0] + (created[1] - created[0]) / 2),
    ]
    thread = _make_thread()
    thread.history.side_effect = lambda *, limit, before=None: _AsyncIter(posts[:limit])
    anthropic = build_stub_anthropic(_reachable_agent_handler(tenant.id, ma_agent_id="ag_target"))
    seeds: list[str] = []

    async def _answer(row: TaskContinuationRow, decision: ContinuationDecision) -> None:
        seeds.append(decision.seed_user_message or "")

    for _ in range(2):  # a second pass answers nothing again
        await dispatch_pending_continuations(
            db_session_factory,
            anthropic,
            tenant_id=tenant.id,
            thread=thread,
            run_follow_up=_answer,
            may_post=_may_post_open,
        )
    assert seeds == works, "one answer per request, oldest first"

    # A message the person sent instead of waiting (no row of its own) supersedes.
    late_key = uuid.uuid4()
    async with db_session_factory() as session, session.begin():
        row = await record_continuation(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="parent-1",
            thread_id="42",
            requester_account_id=account.id,
            requester_external_user_id="555",
            target_ma_agent_id="ag_target",
            target_name="target-agent",
            reason="task_handoff",
            idempotency_key=late_key,
            requested_work="stale work",
        )
    posts.insert(0, _human(13, row.created_at + timedelta(seconds=1)))
    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant.id,
        thread=thread,
        run_follow_up=_answer,
        may_post=_may_post_open,
    )
    assert seeds == works
    async with db_session_factory() as session:
        stale = await get_continuation(session, idempotency_key=late_key)
    assert stale is not None and stale.skip_reason == "skip_superseded"
