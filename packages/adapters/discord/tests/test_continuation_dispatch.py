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
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from daimon.adapters.discord.continuation_dispatch import dispatch_pending_continuations
from daimon.core.continuity.continuation import ContinuationDecision
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
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_stub_anthropic
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


async def _seed_pending_row(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    requested_work: str | None,
    available_at: datetime | None = None,
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
            reason="task_handoff",
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


async def test_a_handoff_survives_any_number_of_busy_binds_as_on_main(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Baseline parity: five busy binds, then a free session, delivers the handoff.

    On main each busy bind settled the row and re-queued the request under a
    new key for the next turn in the thread, with no limit, so the sixth turn
    tail delivered it. Here the same row is released with its claim refunded
    and `available_at` left NULL: still dispatched only at a turn tail, still
    delivered on the sixth, and busy waits never touch the crash budget.
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
            assert row.available_at is None, "a handoff still waits for the next turn tail"
            assert row.started_at is None and row.lease_owner is None

    assert delivered == ["continue"], "the sixth turn tail delivers it, exactly once"
    assert row.status == "delivered" and row.attempts == 1
    assert await list_due_wake_threads(db_session_factory, platform="discord", now=now) == [], (
        "a released handoff is never picked up by the poller; only a turn tail runs it"
    )
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
    assert settled is not None and settled.status == "skipped"
    if may_post:
        thread.send.assert_awaited_once()
    else:
        thread.send.assert_not_called()
