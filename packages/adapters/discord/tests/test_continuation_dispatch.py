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
)
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.task_continuations import (
    get_continuation,
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
        )
    return tenant.id, account.id, "42", idempotency_key


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
    )

    async with db_session_factory() as session:
        settled = await get_continuation(session, idempotency_key=key)
    assert settled is not None
    assert settled.status == "skipped"
    assert settled.skip_reason == "blocked_preparation_failed"


async def test_a_busy_session_releases_the_same_row_for_a_later_retry(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A follow-up that cannot bind because a turn is still running is not lost.

    Nothing ran, and this dispatcher knows it, so the SAME row goes back to
    `pending` a little later instead of being settled. The retry is the same
    key: at-most-once holds because only a claim can run it.
    """
    from daimon.core.turn.errors import SessionBusyError

    tenant_id, _account_id, _thread_id, key = await _seed_pending_row(
        db_session_factory, requested_work="continue"
    )
    thread = _make_thread()
    anthropic = build_stub_anthropic(_reachable_agent_handler(tenant_id, ma_agent_id="ag_target"))
    now = datetime.now(UTC)

    async def _busy_follow_up(row: object, decision: object) -> None:
        raise SessionBusyError(pending_reasons=("agent_identity",), retry_after=now)

    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=_busy_follow_up,
        now=now,
    )

    async with db_session_factory() as session:
        released = await get_continuation(session, idempotency_key=key)
    assert released is not None
    assert released.status == "pending", "the claimed row must not be left claimed"
    assert released.available_at == now + WAKE_RETRY_DELAY
    assert released.started_at is None and released.lease_owner is None

    run_follow_up = AsyncMock()
    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=run_follow_up,
        now=now + timedelta(seconds=1),
    )
    run_follow_up.assert_not_awaited()  # not due yet

    later = now + WAKE_RETRY_DELAY + timedelta(seconds=1)
    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=run_follow_up,
        now=later,
    )
    run_follow_up.assert_awaited_once()
    async with db_session_factory() as session:
        delivered = await get_continuation(session, idempotency_key=key)
    assert delivered is not None and delivered.status == "delivered"
    assert delivered.attempts == 2


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
            now=now,
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
        now=now + timedelta(seconds=1),
    )
    run_follow_up.assert_not_awaited()  # the dead process's lease is still live

    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=run_follow_up,
        now=now + WAKE_CLAIM_LEASE + timedelta(seconds=1),
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
            now=now,
        )

    run_follow_up = AsyncMock()
    after_lease = now + WAKE_RUN_LEASE + timedelta(seconds=1)
    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        tenant_id=tenant_id,
        thread=thread,
        run_follow_up=run_follow_up,
        now=after_lease,
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
