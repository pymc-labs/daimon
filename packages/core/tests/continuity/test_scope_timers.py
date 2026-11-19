"""A due timer in a private DM or channel scope runs through `dispatch_scope_timer`.

Every outcome settles or releases the row; nothing is left to defer forever.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.anthropic_guard import AnthropicDisabled
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.scope_timers import dispatch_scope_timer
from daimon.core.continuity.wakes import enqueue_wake
from daimon.core.errors import DaimonError
from daimon.core.stores.domain import ContinuationReason, TaskContinuationRow
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.turn.errors import (
    AdmissionDenied,
    SessionBusyError,
    SessionPreparationFailed,
    TurnNotStarted,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _seed(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    reason: ContinuationReason = "timer",
    note: str = "stretch your legs",
) -> TaskContinuationRow:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
    request = ContinuationRequest(
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="dm",
        thread_id=f"dm:{uuid.uuid4()}",
        requester_account_id=uuid.uuid4(),
        requester_external_user_id="555",
        target_ma_agent_id="ag_target",
        target_name="target-agent",
        requested_work=note,
        reason=reason,
        idempotency_key=uuid.uuid4(),
    )
    await enqueue_wake(sessionmaker, request, available_at=datetime.now(UTC) - timedelta(seconds=1))
    return await _read(sessionmaker, request.idempotency_key)


async def _read(
    sessionmaker: async_sessionmaker[AsyncSession], key: uuid.UUID
) -> TaskContinuationRow:
    async with sessionmaker() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None
    return row


async def test_a_dm_timer_runs_with_its_note_and_is_delivered(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, note="stretch your legs")
    seeds: list[str] = []

    async def run(_row: TaskContinuationRow, seed: str) -> None:
        seeds.append(seed)

    await dispatch_scope_timer(db_session_factory, row, run_timer=run)

    assert len(seeds) == 1
    assert "stretch your legs" in seeds[0]
    assert " UTC" not in seeds[0], "an imported timer has no set time to show"
    settled = await _read(db_session_factory, row.idempotency_key)
    assert settled.status == "delivered"


async def test_a_second_dispatch_of_a_settled_timer_does_not_run_it_again(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory)
    calls = 0

    async def run(_row: TaskContinuationRow, _seed: str) -> None:
        nonlocal calls
        calls += 1

    await dispatch_scope_timer(db_session_factory, row, run_timer=run)
    await dispatch_scope_timer(db_session_factory, row, run_timer=run)

    assert calls == 1


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (AdmissionDenied(reason="invoker_not_allowed"), "admission_denied:invoker_not_allowed"),
        (SessionPreparationFailed(reasons=("x",), stage="s", retry_after=datetime.now(UTC)), None),
        (TurnNotStarted("stopped"), "not_started:stopped"),
        (DaimonError("boom"), "dispatch_failed"),
    ],
)
async def test_a_refused_dm_timer_settles_skipped_and_never_retries(
    db_session_factory: async_sessionmaker[AsyncSession],
    error: Exception,
    reason: str | None,
) -> None:
    row = await _seed(db_session_factory)

    async def run(_row: TaskContinuationRow, _seed: str) -> None:
        raise error

    await dispatch_scope_timer(db_session_factory, row, run_timer=run)

    settled = await _read(db_session_factory, row.idempotency_key)
    assert settled.status == "skipped"
    if reason is not None:
        assert settled.skip_reason == reason


async def test_a_timer_that_hit_a_full_queue_is_offered_again(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory)

    async def run(_row: TaskContinuationRow, _seed: str) -> None:
        raise TurnNotStarted("queue_full")

    await dispatch_scope_timer(db_session_factory, row, run_timer=run)

    again = await _read(db_session_factory, row.idempotency_key)
    assert again.status == "pending"
    assert again.available_at is not None and again.available_at > datetime.now(UTC)


async def test_a_busy_session_refunds_the_attempt_and_defers(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory)
    retry_after = datetime.now(UTC) + timedelta(minutes=3)

    async def run(_row: TaskContinuationRow, _seed: str) -> None:
        raise SessionBusyError(pending_reasons=("timer",), retry_after=retry_after)

    await dispatch_scope_timer(db_session_factory, row, run_timer=run)

    again = await _read(db_session_factory, row.idempotency_key)
    assert again.status == "pending"
    assert again.available_at is not None and again.available_at >= retry_after
    assert again.attempts == row.attempts


async def test_a_disabled_provider_releases_the_timer(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory)

    async def run(_row: TaskContinuationRow, _seed: str) -> None:
        raise AnthropicDisabled("off")

    await dispatch_scope_timer(db_session_factory, row, run_timer=run)

    again = await _read(db_session_factory, row.idempotency_key)
    assert again.status == "pending"


async def test_only_timers_run_in_a_dm_scope(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, reason="task_handoff")

    async def run(_row: TaskContinuationRow, _seed: str) -> None:
        raise AssertionError("must not run")

    with pytest.raises(ValueError, match="timers"):
        await dispatch_scope_timer(db_session_factory, row, run_timer=run)
