"""The platform-neutral continuation loop: every claimed row settles or is released."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic
from daimon.core.continuity.continuation import (
    ContinuationRequest,
    ResponderChanged,
    record_continuation,
)
from daimon.core.continuity.dispatch import dispatch_pending_continuations
from daimon.core.continuity.wakes import WAKE_RETRY_DELAY, enqueue_wake
from daimon.core.stores.domain import ContinuationReason, TaskContinuationRow
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_sessions import mark_turn_active
from daimon.core.turn.errors import AdmissionDenied, SessionBusyError, SessionPreparationFailed
from daimon.core.turn_origin import build_handoff_notice
from daimon.testing.factories import make_account, make_tenant, make_thread_session
from daimon.testing.ma import MARouter, build_fake_anthropic
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TARGET = "agt_stats"
_THREAD = "19:abc@thread.tacv2;messageid=1"
_SOON = datetime.now(UTC) + timedelta(minutes=1)


@pytest_asyncio.fixture
async def caller(db_session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = await make_tenant(db_session, platform="teams")
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    return tenant.id, account.id


def _anthropic(tenant_id: uuid.UUID) -> AsyncAnthropic:
    router = MARouter()
    router.add_agent(
        ma_agent(
            id=_TARGET,
            name="stats-bot",
            model="m",
            tenant_id=tenant_id,
            created_at=datetime.now(UTC),
        )
    )
    return build_fake_anthropic(router.dispatch)


async def _queue(
    factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
    work: str | None,
    *,
    reason: ContinuationReason = "task_handoff",
    available_at: datetime | None = None,
) -> uuid.UUID:
    """A handoff, or with `available_at` a due wake."""
    key = uuid.uuid4()
    request = ContinuationRequest(
        tenant_id=caller[0],
        platform="teams",
        parent_channel_id="19:abc@thread.tacv2",
        thread_id=_THREAD,
        requester_account_id=caller[1],
        requester_external_user_id="entra-oid",
        target_ma_agent_id=_TARGET,
        target_name="stats-bot",
        requested_work=work,
        reason=reason,
        idempotency_key=key,
    )
    if available_at is None:
        await record_continuation(factory, request)
    else:
        await enqueue_wake(factory, request, available_at=available_at)
    return key


async def _dispatch(
    factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
    *,
    run: Exception | None = None,
    latest: datetime | None = None,
) -> tuple[list[str], list[str]]:
    seeds: list[str] = []
    notices: list[str] = []

    async def run_follow_up(_row: TaskContinuationRow, seed: str) -> None:
        seeds.append(seed)
        if run is not None:
            raise run

    async def post_notice(text: str) -> None:
        notices.append(text)

    async def latest_user_message_at(_row: TaskContinuationRow) -> datetime | None:
        return latest

    await dispatch_pending_continuations(
        factory,
        _anthropic(caller[0]),
        tenant_id=caller[0],
        platform="teams",
        thread_id=_THREAD,
        run_follow_up=run_follow_up,
        post_notice=post_notice,
        latest_user_message_at=latest_user_message_at,
        dispatch_errors=(RuntimeError,),
    )
    return seeds, notices


async def _row(factory: async_sessionmaker[AsyncSession], key: uuid.UUID) -> TaskContinuationRow:
    async with factory() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None, "the continuation row should exist"
    return row


async def _status(
    factory: async_sessionmaker[AsyncSession], key: uuid.UUID
) -> tuple[str, str | None]:
    row = await _row(factory, key)
    return row.status, row.skip_reason


async def test_dispatches_once_and_settles_delivered(
    db_session_factory: async_sessionmaker[AsyncSession], caller: tuple[uuid.UUID, uuid.UUID]
) -> None:
    key = await _queue(db_session_factory, caller, "finish the writeup")
    assert await _dispatch(db_session_factory, caller) == (["finish the writeup"], [])
    assert await _status(db_session_factory, key) == ("delivered", None)
    assert await _dispatch(db_session_factory, caller) == ([], []), "a settled row never reruns"


@pytest.mark.parametrize(
    ("work", "latest", "reason"),
    [
        (None, None, "skip_save_only"),
        ("w", datetime.now(UTC) + timedelta(days=1), "skip_superseded"),
    ],
)
async def test_skips_settle_without_running(
    db_session_factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
    work: str | None,
    latest: datetime | None,
    reason: str,
) -> None:
    key = await _queue(db_session_factory, caller, work)
    assert await _dispatch(db_session_factory, caller, latest=latest) == ([], [])
    assert await _status(db_session_factory, key) == ("skipped", reason)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            SessionPreparationFailed(reasons=("x",), stage="bind", retry_after=_SOON),
            "blocked_preparation_failed",
        ),
        (RuntimeError("card post failed"), "dispatch_failed"),
        (AdmissionDenied(reason="balance_depleted"), "admission_denied:balance_depleted"),
    ],
)
async def test_run_failures_settle(
    db_session_factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
    error: Exception,
    reason: str,
) -> None:
    key = await _queue(db_session_factory, caller, "w")
    await _dispatch(db_session_factory, caller, run=error)
    assert await _status(db_session_factory, key) == ("skipped", reason)


async def test_a_busy_handoff_is_released_for_the_next_turn_tail(
    db_session_factory: async_sessionmaker[AsyncSession], caller: tuple[uuid.UUID, uuid.UUID]
) -> None:
    key = await _queue(db_session_factory, caller, "w")
    busy = SessionBusyError(pending_reasons=("x",), retry_after=_SOON)
    await _dispatch(db_session_factory, caller, run=busy)
    row = await _row(db_session_factory, key)
    assert (row.status, row.attempts, row.available_at) == ("pending", 0, None), (
        "the same row waits again, claim refunded, still a handoff"
    )
    assert await _dispatch(db_session_factory, caller) == (["w"], []), "the next tail runs it"
    assert await _status(db_session_factory, key) == ("delivered", None)


async def test_a_busy_wake_is_released_until_the_session_frees(
    db_session_factory: async_sessionmaker[AsyncSession], caller: tuple[uuid.UUID, uuid.UUID]
) -> None:
    key = await _queue(
        db_session_factory, caller, "w", reason="timer", available_at=datetime.now(UTC)
    )
    later = datetime.now(UTC) + timedelta(minutes=10)
    await _dispatch(
        db_session_factory, caller, run=SessionBusyError(pending_reasons=("x",), retry_after=later)
    )
    row = await _row(db_session_factory, key)
    assert (row.status, row.attempts, row.available_at) == ("pending", 0, later), (
        "a wake is retried no earlier than the busy session's retry_after"
    )


async def test_a_wake_behind_a_running_turn_is_released_without_a_notice(
    db_session_factory: async_sessionmaker[AsyncSession], caller: tuple[uuid.UUID, uuid.UUID]
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await get_tenant(session, caller[0])
        assert tenant is not None
        account = await make_account(session, tenant=tenant)
        live = await make_thread_session(
            session, tenant=tenant, account=account, platform="teams", thread_id=_THREAD
        )
        await mark_turn_active(session, id=live.id, active_turn_message_id="m-1", now=_SOON)
    caller = (caller[0], account.id)
    key = await _queue(
        db_session_factory, caller, "w", reason="timer", available_at=datetime.now(UTC)
    )
    before = datetime.now(UTC)
    assert await _dispatch(db_session_factory, caller) == ([], []), "nobody is told"
    row = await _row(db_session_factory, key)
    assert row.status == "pending" and row.available_at is not None
    assert row.available_at >= before + WAKE_RETRY_DELAY, "retried after the turn in progress"


async def test_a_changed_timer_responder_posts_the_notice_and_settles(
    db_session_factory: async_sessionmaker[AsyncSession], caller: tuple[uuid.UUID, uuid.UUID]
) -> None:
    key = await _queue(
        db_session_factory, caller, "w", reason="timer", available_at=datetime.now(UTC)
    )
    changed = ResponderChanged(target_name="stats-bot", current_name="daimon")
    _, notices = await _dispatch(db_session_factory, caller, run=changed)
    assert notices == [changed.message], "the thread is told the timer did not run"
    assert await _status(db_session_factory, key) == ("skipped", "skip_target_changed")


@pytest.mark.parametrize(
    ("kind", "workspace", "not_carried"),
    [
        ("full", "transferred", ()),
        ("transcript", "transcript_only", ("working files",)),
        (None, "history_only", ("working files", "earlier conversation")),
    ],
)
def test_handoff_notice_claims_only_what_carried(
    kind: str | None, workspace: str, not_carried: tuple[str, ...]
) -> None:
    notice = build_handoff_notice(
        from_name=None,
        from_ma_agent_id=None,
        requested_by="the requester",
        requested_work="w",
        transfer_kind=kind,  # type: ignore[arg-type]
    )
    assert (notice.workspace, notice.not_carried) == (workspace, not_carried)
    assert (notice.from_name, notice.from_ma_agent_id) == ("the previous agent", "")
