"""Batched retention deletes against real Postgres: what goes and what stays."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core._models import (
    CredentialRequest,
    McpOAuthFlow,
    Routine,
    SessionSettleState,
    TaskContinuation,
    ThreadSession,
    TurnOutcome,
    Watch,
)
from daimon.core.credential_requests import mint_request_token
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores import retention, turn_outcomes
from daimon.core.stores import tenants as tenants_store
from daimon.core.turn.termination import TerminationReason
from daimon.testing.factories import (
    make_account,
    make_routine,
    make_task_continuation,
    make_tenant,
    make_usage_event,
)
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
