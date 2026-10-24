"""The platform-neutral continuation loop: every claimed row settles or is released."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.continuity.continuation import (
    ContinuationRequest,
    ResponderChanged,
    record_continuation,
)
from daimon.core.continuity.dispatch import (
    WorkerFinishedDecision,
    dispatch_pending_continuations,
)
from daimon.core.continuity.wakes import WAKE_RETRY_DELAY, enqueue_wake
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import ContinuationReason, TaskContinuationRow
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_sessions import clear_active_turn, mark_turn_active
from daimon.core.turn.driver import SEND_ATTEMPTED
from daimon.core.turn.errors import AdmissionDenied, SessionBusyError, SessionPreparationFailed
from daimon.core.turn_origin import build_handoff_notice
from daimon.testing.factories import make_account, make_tenant, make_thread_session
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from sqlalchemy.exc import OperationalError
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
    target_tenant: uuid.UUID | None = None,
    worker_finished_decision: WorkerFinishedDecision | None = None,
    now: datetime | None = None,
    sends: bool = False,
) -> tuple[list[str], list[str]]:
    """`target_tenant` owns the target agent: another tenant's means it is gone."""
    seeds: list[str] = []
    notices: list[str] = []

    async def run_follow_up(_row: TaskContinuationRow, seed: str) -> None:
        seeds.append(seed)
        if sends:
            # As `run_prepared_turn` does once the message is handed to the provider.
            mark = SEND_ATTEMPTED.get()
            assert mark is not None, "the dispatcher watches for a send"
            mark.append(True)
        if run is not None:
            raise run

    async def post_notice(text: str) -> None:
        notices.append(text)

    async def latest_user_message_at(_row: TaskContinuationRow) -> datetime | None:
        return latest

    await dispatch_pending_continuations(
        factory,
        _anthropic(target_tenant or caller[0]),
        tenant_id=caller[0],
        platform="teams",
        thread_id=_THREAD,
        run_follow_up=run_follow_up,
        post_notice=post_notice,
        latest_user_message_at=latest_user_message_at,
        dispatch_errors=(RuntimeError,),
        worker_finished_decision=worker_finished_decision,
        **({} if now is None else {"now": lambda: now}),
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


async def test_worker_completion_hook_does_not_intercept_a_handoff(
    db_session_factory: async_sessionmaker[AsyncSession], caller: tuple[uuid.UUID, uuid.UUID]
) -> None:
    async def worker_finished(_row: TaskContinuationRow, _now: datetime) -> None:
        raise AssertionError("A handoff must use its existing history and agent checks")

    key = await _queue(db_session_factory, caller, "finish the writeup")
    assert await _dispatch(
        db_session_factory, caller, worker_finished_decision=worker_finished
    ) == (["finish the writeup"], [])
    assert await _status(db_session_factory, key) == ("delivered", None)


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


@pytest.mark.parametrize("wrapped", [False, True])
async def test_a_busy_handoff_waits_for_the_poller_not_a_turn_tail(
    db_session_factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
    wrapped: bool,
) -> None:
    """J6g/M07g (#1549 review 1): a busy bind before the send releases the hand-off with a
    poll time, direct or wrapped in the turn's own TurnError. Left with no
    `available_at` it waited for a turn tail that may never come: the copy is the
    one meant to answer, so the wake poller delivers it."""
    from daimon.core.continuity.wakes import list_due_wake_threads
    from daimon.core.errors import TurnError

    key = await _queue(db_session_factory, caller, "w")
    soon = datetime.now(UTC) + timedelta(minutes=5)  # later than the poll delay
    busy = SessionBusyError(pending_reasons=("x",), retry_after=soon)
    await _dispatch(
        db_session_factory, caller, run=TurnError(kind="upstream", cause=busy) if wrapped else busy
    )
    row = await _row(db_session_factory, key)
    assert (row.status, row.attempts, row.available_at) == ("pending", 0, soon), (
        "the same row waits, claim refunded, until the session's own retry time"
    )
    due = await list_due_wake_threads(db_session_factory, platform="teams", now=soon)
    assert [(t.tenant_id, t.thread_id) for t in due if t.tenant_id == caller[0]] == [
        (caller[0], _THREAD)
    ], "the poller picks it up; no other turn in the thread is needed"
    assert await _dispatch(db_session_factory, caller, now=soon) == (["w"], [])
    assert await _status(db_session_factory, key) == ("delivered", None)
    assert await _dispatch(db_session_factory, caller, now=soon) == ([], []), "answered once"


async def test_a_busy_error_after_the_send_is_settled_never_rerun(
    db_session_factory: async_sessionmaker[AsyncSession], caller: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """#1549 review 5: a busy error raised once the message may have reached the session
    (a mid-turn session replacement) is not "nothing ran": re-running it could answer
    twice, so the row settles instead of waiting."""
    from daimon.core.errors import TurnError

    key = await _queue(db_session_factory, caller, "w")
    busy = SessionBusyError(pending_reasons=("seal",), retry_after=_SOON)
    assert await _dispatch(
        db_session_factory, caller, run=TurnError(kind="upstream", cause=busy), sends=True
    ) == (["w"], [])
    assert await _status(db_session_factory, key) == ("skipped", "dispatch_failed")
    assert await _dispatch(db_session_factory, caller, now=_SOON) == ([], []), "never rerun"


async def test_a_handoff_held_up_past_the_cap_is_told_once_and_stops(
    db_session_factory: async_sessionmaker[AsyncSession], caller: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """#1549 review 3: a release refunds the attempt, so a hand-off whose session never
    frees was retried forever. Past `HANDOFF_WAIT_CAP` the person is told plainly,
    once, and the row settles."""
    from daimon.core.continuity.dispatch import HANDOFF_WAIT_CAP
    from daimon.core.continuity.messages import render_handoff_wait_expired

    key = await _queue(db_session_factory, caller, "w")
    created = (await _row(db_session_factory, key)).created_at
    busy = SessionBusyError(pending_reasons=("x",), retry_after=created)
    late = created + HANDOFF_WAIT_CAP
    assert await _dispatch(db_session_factory, caller, run=busy, now=late) == (
        ["w"],
        [render_handoff_wait_expired("stats-bot")],
    )
    assert await _status(db_session_factory, key) == ("skipped", "skip_wait_expired")
    assert await _dispatch(db_session_factory, caller, now=late + timedelta(minutes=1)) == (
        [],
        [],
    ), "told once"


async def test_a_message_queued_behind_a_hand_off_is_answered_exactly_once(
    db_session_factory: async_sessionmaker[AsyncSession], caller: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """M07g: the person writes again while the hand-off's turn still runs.

    Their message is queued for the copy after the hand-off's own work, so it
    supersedes that work: the copy answers the newer message once and the stale
    request never runs, now or at a later turn tail.
    """
    handed = await _queue(db_session_factory, caller, "summarise the thread for me")
    queued = await _queue(db_session_factory, caller, "which model are you running on?")
    first, second = await _row(db_session_factory, handed), await _row(db_session_factory, queued)
    assert first.created_at < second.created_at
    said_at = first.created_at + (second.created_at - first.created_at) / 2

    assert await _dispatch(db_session_factory, caller, latest=said_at) == (
        ["which model are you running on?"],
        [],
    )
    assert await _status(db_session_factory, handed) == ("skipped", "skip_superseded")
    assert await _status(db_session_factory, queued) == ("delivered", None)
    assert await _dispatch(db_session_factory, caller, latest=said_at) == ([], []), (
        "nothing answers a second time"
    )


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


async def test_a_handoff_behind_a_running_turn_is_retried_not_settled(
    db_session_factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """J6g: the hand-off's queued work was recorded by the turn still marked
    active. It must wait for that turn and then run, not settle on the notice."""
    async with db_session_factory.begin() as session:
        tenant = await get_tenant(session, caller[0])
        assert tenant is not None
        account = await make_account(session, tenant=tenant)
        live = await make_thread_session(
            session, tenant=tenant, account=account, platform="teams", thread_id=_THREAD
        )
        await mark_turn_active(session, id=live.id, active_turn_message_id="m-1", now=_SOON)
    caller = (caller[0], account.id)
    monkeypatch.setattr("daimon.core.continuity.dispatch.WAKE_RETRY_DELAY", timedelta(0))
    key = await _queue(db_session_factory, caller, "w")
    assert await _dispatch(db_session_factory, caller) == ([], []), "nobody is told"
    row = await _row(db_session_factory, key)
    assert (row.status, row.available_at is not None) == ("pending", True), (
        "the handoff waits for the turn in progress instead of settling"
    )
    async with db_session_factory.begin() as session:
        await clear_active_turn(session, id=live.id)
    assert await _dispatch(db_session_factory, caller) == (["w"], []), "it runs after that turn"
    assert await _status(db_session_factory, key) == ("delivered", None)


@pytest.mark.parametrize("path", ["responder_changed", "target_gone"])
@pytest.mark.parametrize("target", ["protected", "unknown", "open"])
async def test_notices_reach_only_a_thread_the_agent_may_post_in(
    db_session_factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    target: str,
) -> None:
    """SYS-048: a protected thread, or one whose protection can't be read, gets
    no skip or responder-changed notice; the row settles either way."""
    if target == "protected":
        policy = TenantAccessPolicy(protected_channel_ids=("19:abc@thread.tacv2",))
        async with db_session_factory.begin() as session:
            await set_access_policy(session, tenant_id=caller[0], policy=policy)
    if target == "unknown":

        async def _policy_read_fails(*_args: object, **_kwargs: object) -> object:
            raise OperationalError("SELECT", {}, Exception("pool gone"))

        monkeypatch.setattr("daimon.core.turn.protection.load_access_policy", _policy_read_fails)
    key = await _queue(
        db_session_factory, caller, "w", reason="timer", available_at=datetime.now(UTC)
    )
    if path == "responder_changed":
        run = ResponderChanged(target_name="stats-bot", current_name="daimon")
        _, notices = await _dispatch(db_session_factory, caller, run=run)
    else:
        _, notices = await _dispatch(db_session_factory, caller, target_tenant=uuid.uuid4())
    assert await _status(db_session_factory, key) == ("skipped", "skip_target_changed")
    assert bool(notices) is (target == "open"), f"{target} thread: notices {notices}"


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
