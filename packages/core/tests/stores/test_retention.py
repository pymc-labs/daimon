"""Batched retention deletes against real Postgres: what goes and what stays."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from daimon.core._models import (
    CredentialRequest,
    GitHubAccessRequest,
    LongJobCancelAuditRecord,
    LongJobRecord,
    McpOAuthFlow,
    Routine,
    SessionSettleState,
    TaskContinuation,
    ThreadSession,
    TurnOutcome,
    Watch,
    WatchFiring,
)
from daimon.core.credential_requests import mint_request_token
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores import retention, turn_outcomes
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import watches as watches_store
from daimon.core.turn.termination import TerminationReason
from daimon.testing.factories import (
    make_account,
    make_routine,
    make_task_continuation,
    make_tenant,
    make_usage_event,
)
from daimon.testing.watch_fixtures import install_firing_trigger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 6, 1, tzinfo=UTC)
OLD = NOW - timedelta(days=200)


async def _request(
    session: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID, *, flow: str | None
) -> str:
    request = await requests_store.create_credential_request(
        session,
        token=mint_request_token(),
        kind="mcp_oauth",
        tenant_id=tenant_id,
        agent_id=uuid.uuid4(),
        account_id=account_id,
        target="notes",
        mcp_server_url="https://mcp.example.com/mcp",
        requester_platform_user_id="requester-1",
        channel_id="chan-1",
        expires_at=OLD,
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_test",
        target_name="daimon",
        requested_work=None,
    )
    await session.execute(
        update(CredentialRequest)
        .where(CredentialRequest.token == request.token)
        .values(used_at=OLD)
    )
    if flow is not None:
        await flows_store.create_flow(
            session,
            state="st_" + uuid.uuid4().hex,
            request_token=request.token,
            tenant_id=tenant_id,
            account_id=account_id,
            agent_id=request.agent_id,
            server_name="notes",
            mcp_server_url="https://mcp.example.com/mcp",
            redirect_uri="https://d.example/oauth/mcp/callback",
            code_verifier="verifier",
            expires_at=OLD,
        )
        if flow == "completed":
            await session.execute(
                update(McpOAuthFlow)
                .where(McpOAuthFlow.request_token == request.token)
                .values(used_at=OLD, completed_at=OLD)
            )
    return request.token


async def test_credential_sweep_keeps_requests_behind_completed_sign_ins(
    db_session: AsyncSession,
) -> None:
    """A completed MCP sign-in cascades from its request, so the request must stay."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    signed_in = await _request(db_session, tenant.id, account.id, flow="completed")
    abandoned = await _request(db_session, tenant.id, account.id, flow="started")
    plain = await _request(db_session, tenant.id, account.id, flow=None)

    removed = await retention.delete_credential_requests(db_session, before=NOW, limit=100)

    tokens = set(await db_session.scalars(select(CredentialRequest.token)))
    assert removed == 2, "the abandoned flow's request and the plain request go"
    assert tokens == {signed_in}, "only the request behind a completed sign-in stays"
    flows = set(await db_session.scalars(select(McpOAuthFlow.request_token)))
    assert flows == {signed_in}, "the completed flow survives; the abandoned one cascades away"
    assert abandoned not in tokens and plain not in tokens, "used requests past the cutoff go"


def _mapping(tenant_id: uuid.UUID, thread: str, *, platform: str = "discord") -> ThreadSession:
    return ThreadSession(
        tenant_id=tenant_id,
        platform=platform,
        thread_id=thread,
        ma_session_id=f"sesn_{thread}",
        last_turn_at=OLD,
    )


async def test_idle_sweep_keeps_threads_that_work_still_targets(db_session: AsyncSession) -> None:
    """Pending continuations, live watches and enabled routines keep their thread."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    threads = ["plain", "pending", "delivered", "watched", "ended-watch", "routine"]
    db_session.add_all([_mapping(tenant.id, t) for t in threads])
    db_session.add(_mapping(tenant.id, "1700.1", platform="slack"))
    for thread in ("pending", "delivered"):
        row = await make_task_continuation(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="c1",
            thread_id=thread,
            requester_account_id=account.id,
            requester_external_user_id="u1",
            target_ma_agent_id="ag_test",
            target_name="daimon",
            reason="timer",
            idempotency_key=uuid.uuid4(),
        )
        if thread == "delivered":
            await db_session.execute(
                update(TaskContinuation)
                .where(TaskContinuation.id == row.id)
                .values(status="delivered", delivered_at=NOW)
            )
    for thread, status in (("watched", "active"), ("ended-watch", "expired")):
        db_session.add(
            Watch(
                tenant_id=tenant.id,
                owner_account_id=account.id,
                owner_external_id="u1",
                platform="discord",
                parent_channel_id="c1",
                thread_id=thread,
                agent_id="ag_test",
                agent_name="daimon",
                session_id=f"sesn_{thread}",
                instructions="watch",
                producer=f"p-{thread}",
                page_id=uuid.uuid4(),
                status=status,
                expires_at=NOW + timedelta(days=7),
            )
        )
    for destination in ("routine", "C1:1700.1"):
        routine = await make_routine(db_session, tenant=tenant)
        await db_session.execute(
            update(Routine)
            .where(Routine.id == routine.id)
            .values(destination_kind="thread", destination_id=destination)
        )
    await db_session.flush()

    await retention.delete_idle_thread_sessions(
        db_session, tenant_id=tenant.id, before=NOW, limit=100
    )

    left = set(await db_session.scalars(select(ThreadSession.thread_id)))
    assert left == {"pending", "watched", "routine", "1700.1"}, (
        "idle threads with a pending continuation, a live watch or an enabled routine stay; "
        "plain, delivered and ended-watch threads go"
    )


def _outcome(
    tenant_id: uuid.UUID, session_id: str | None, *, started_at: datetime = NOW
) -> turn_outcomes.OutcomeRecord:
    return turn_outcomes.OutcomeRecord(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        account_id=None,
        platform="scheduler",
        channel_id=None,
        thread_id=None,
        agent_id=None,
        session_id=session_id,
        origin="routine",
        reason=TerminationReason.COMPLETED,
        started_at=started_at - timedelta(minutes=1),
        ended_at=started_at,
        duration_ms=60_000,
        recovered=False,
        error_class=None,
        release="test",
        usage_refs=[],
    )


async def test_any_turn_outcome_marks_its_conversation_used(db_session: AsyncSession) -> None:
    """A routine turn, which never takes the chat path, still keeps its thread alive."""
    tenant = await make_tenant(db_session)
    db_session.add_all([_mapping(tenant.id, "used"), _mapping(tenant.id, "idle")])
    await db_session.flush()

    await turn_outcomes.record(db_session, _outcome(tenant.id, "sesn_used"))
    await retention.delete_idle_thread_sessions(
        db_session, tenant_id=tenant.id, before=NOW - timedelta(days=1), limit=100
    )

    last = await db_session.scalar(
        select(ThreadSession.last_turn_at).where(ThreadSession.thread_id == "used")
    )
    assert last == NOW, "the outcome moves last_turn_at to when the turn ended"
    left = set(await db_session.scalars(select(ThreadSession.thread_id)))
    assert left == {"used"}, "only the mapping without a recent turn goes"


async def test_turn_outcomes_go_with_their_conversation_once_settled(
    db_session: AsyncSession,
) -> None:
    """An old outcome stays while its conversation lives or its usage awaits a settle."""
    tenant = await make_tenant(db_session)
    db_session.add(_mapping(tenant.id, "live"))
    db_session.add(SessionSettleState(managed_session_id="sesn_unsettled", settle_requested_at=OLD))
    await db_session.flush()
    sessions = ["sesn_live", "sesn_unsettled", "sesn_gone", None]
    for session_id in sessions:
        await turn_outcomes.record(db_session, _outcome(tenant.id, session_id, started_at=OLD))
    await turn_outcomes.record(db_session, _outcome(tenant.id, "sesn_recent"))

    await retention.delete_turn_outcomes(
        db_session, tenant_id=tenant.id, before=NOW - timedelta(days=30), limit=100
    )

    left = set(await db_session.scalars(select(TurnOutcome.session_id)))
    assert left == {"sesn_live", "sesn_unsettled", "sesn_recent"}, (
        "outcomes of a live conversation, an unsettled session or a recent turn stay; "
        "an old one whose conversation is gone goes"
    )


async def test_tenant_deletion_finds_settle_state_without_outcomes(
    db_session: AsyncSession,
) -> None:
    """Settle progress of a session only usage events name still goes with the tenant."""
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    await make_usage_event(db_session, tenant=tenant, managed_session_id="sesn_billed")
    await make_usage_event(db_session, tenant=other, managed_session_id="sesn_other")
    db_session.add_all(
        [
            SessionSettleState(managed_session_id="sesn_billed", settle_requested_at=OLD),
            SessionSettleState(managed_session_id="sesn_other", settle_requested_at=OLD),
        ]
    )
    await db_session.flush()

    await tenants_store.delete_settle_state(db_session, tenant_id=tenant.id)

    left = set(await db_session.scalars(select(SessionSettleState.managed_session_id)))
    assert left == {"sesn_other"}, "the tenant's settle row goes, another tenant's stays"


def _access_request(
    tenant_id: uuid.UUID, account_id: uuid.UUID, status: str, **values: object
) -> GitHubAccessRequest:
    return GitHubAccessRequest(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        requester_account_id=account_id,
        requester_platform_user_id=f"u-{status}",
        platform="discord",
        parent_channel_id="c",
        thread_id=f"t-{status}-{uuid.uuid4().hex[:6]}",
        agent_id=uuid.uuid4(),
        ma_agent_id="ag_test",
        agent_name="daimon",
        repo_names=["org/repo"],
        status=status,
        updated_at=OLD,
        expires_at=OLD,
        **values,
    )


async def test_access_requests_go_only_when_finished_and_nothing_still_needs_them(
    db_session: AsyncSession,
) -> None:
    """Past expiry alone is not enough: the expiry poller and its notice come first."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session)
    db_session.add_all(
        [
            _access_request(tenant.id, account.id, "open"),
            _access_request(tenant.id, account.id, "waiting_github"),
            _access_request(tenant.id, account.id, "expired"),
            _access_request(tenant.id, account.id, "expired", expiry_notice_sent_at=OLD),
            _access_request(tenant.id, account.id, "ready"),
            _access_request(tenant.id, account.id, "cancelled"),
            _access_request(tenant.id, account.id, "declined"),
        ]
    )
    await db_session.flush()
    kept = set(
        await db_session.scalars(
            select(GitHubAccessRequest.id).where(
                (GitHubAccessRequest.status.in_(("open", "waiting_github")))
                | (
                    (GitHubAccessRequest.status == "expired")
                    & GitHubAccessRequest.expiry_notice_sent_at.is_(None)
                )
            )
        )
    )

    removed = await retention.delete_github_access_requests(db_session, before=NOW, limit=100)

    assert removed == 4, "ready, cancelled, declined and a noticed expiry go"
    left = set(await db_session.scalars(select(GitHubAccessRequest.id)))
    assert left == kept, "open, waiting and an expiry with no notice yet stay"


async def test_cancel_audit_outlives_its_job_and_ages_on_its_own(db_session: AsyncSession) -> None:
    """Deleting a finished job on the output clock must not take its audit row with it."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session)
    job = LongJobRecord(
        tenant_id=tenant.id,
        owner_account_id=account.id,
        owner_external_user_id="u1",
        agent_id="ag_test",
        agent_name="daimon",
        agent_version=1,
        environment_id="env",
        environment_updated_at=OLD,
        platform="discord",
        parent_channel_id="c",
        thread_id="t",
        parent_session_id="sesn_1",
        tool_use_id="tu_1",
        command="true",
        title="job",
        deadline_minutes=5,
        max_usd=Decimal("1"),
        state="succeeded",
        finished_at=OLD,
    )
    db_session.add(job)
    await db_session.flush()
    for requested_at, id_ in ((OLD, job.id), (NOW - timedelta(days=1), uuid.uuid4())):
        db_session.add(
            LongJobCancelAuditRecord(
                tenant_id=tenant.id,
                id=id_,
                state="running",
                owner_account_id=account.id,
                requested_by=account.id,
                requested_at=requested_at,
            )
        )
    await db_session.flush()

    assert await retention.delete_long_jobs(db_session, before=NOW, limit=100) == 1
    audit = (await db_session.scalars(select(LongJobCancelAuditRecord.requested_at))).all()
    assert len(audit) == 2, "the job's cancel audit stays when the job goes"

    removed = await retention.delete_long_job_cancel_audit(
        db_session, before=NOW - timedelta(days=90), limit=100
    )

    assert removed == 1, "the audit row past its own clock goes"
    left = (await db_session.scalars(select(LongJobCancelAuditRecord.requested_at))).all()
    assert left == [NOW - timedelta(days=1)], "a recent audit row stays"


async def test_a_watch_keeps_its_last_firing_after_the_wake_is_swept(
    db_session: AsyncSession,
) -> None:
    """The firing rows go with their wake; the live watch still reports its last firing."""
    await install_firing_trigger(db_session)
    tenant = await make_tenant(db_session)
    account = await make_account(db_session)
    watch = Watch(
        tenant_id=tenant.id,
        owner_account_id=account.id,
        owner_external_id="u1",
        platform="discord",
        parent_channel_id="c1",
        thread_id="t1",
        agent_id="ag_test",
        agent_name="daimon",
        session_id="sesn_t1",
        instructions="watch",
        producer="p-last-fired",
        page_id=uuid.uuid4(),
        status="active",
        expires_at=NOW + timedelta(days=7),
    )
    db_session.add(watch)
    await db_session.flush()
    for age, key in ((60, uuid.uuid4()), (40, uuid.uuid4())):
        fired = NOW - timedelta(days=age)
        await make_task_continuation(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="c1",
            thread_id="t1",
            requester_account_id=account.id,
            requester_external_user_id="u1",
            target_ma_agent_id="ag_test",
            target_name="daimon",
            reason="watch_fired",
            idempotency_key=key,
            created_at=fired,
        )
        await db_session.execute(
            update(TaskContinuation)
            .where(TaskContinuation.idempotency_key == key)
            .values(status="delivered", delivered_at=fired)
        )
        db_session.add(
            WatchFiring(wake_key=key, watch_id=watch.id, revision=1, generation=1, created_at=fired)
        )
        await db_session.flush()

    removed = await retention.delete_task_continuations(db_session, before=NOW, limit=100)

    assert removed == 2, "both delivered wakes are past their clock"
    assert await db_session.scalar(select(WatchFiring.wake_key).limit(1)) is None, (
        "the firing rows go with the wakes"
    )
    db_session.expire_all()  # the trigger wrote the column behind the session's back
    activity = await watches_store.list_tenant_watches(db_session, tenant_id=tenant.id, now=NOW)
    assert [a.last_fired_at for a in activity] == [NOW - timedelta(days=40)], (
        "the watch keeps its latest firing time"
    )
