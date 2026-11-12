"""Batched retention deletes for records kept for a bounded time.

Each function removes at most `limit` rows in the caller's transaction and returns
how many it removed, so a sweep can loop in short transactions until a batch
comes back short. Every cutoff is exclusive and timezone-aware.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from daimon.core._models import (
    ChannelChange,
    ComputeTaskRecord,
    CredentialRequest,
    GitHubAccessRequest,
    LongJobRecord,
    McpOAuthFlow,
    MessageFeedback,
    PlatformChannelName,
    Routine,
    SupportEscalation,
    TaskContinuation,
    Tenant,
    ThreadSession,
    TurnOutcome,
    Watch,
    WebAudit,
    WebIdentity,
    WebLogin,
    WebLoginAccount,
    WebSession,
    WebThread,
)
from sqlalchemy import (
    ColumnElement,
    and_,
    delete,
    exists,
    func,
    literal_column,
    or_,
    select,
    update,
)
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnClause

_FINISHED_JOB_STATES = ("succeeded", "failed", "timed_out", "cancelled", "lost", "over_budget")
_CLOSED_CONTINUATIONS = ("delivered", "skipped", "cancelled")
_OPEN_CONTINUATIONS = ("pending", "claimed")
_LIVE_WATCHES = ("active", "paused")
_CLOSED_ACCESS_REQUESTS = ("cancelled", "declined", "expired")


def _check(before: datetime) -> None:
    if before.utcoffset() is None:
        raise ValueError("cutoff must include a timezone")


async def _delete_batch(
    session: AsyncSession, model: type[Any], condition: ColumnElement[bool], limit: int
) -> int:
    ctid: ColumnClause[Any] = literal_column("ctid")
    batch = select(ctid).select_from(model).where(condition).limit(limit)
    result = await session.execute(delete(model).where(ctid.in_(batch)))
    return cast(CursorResult[Any], result).rowcount


async def _update_batch(
    session: AsyncSession,
    model: type[Any],
    condition: ColumnElement[bool],
    values: dict[str, Any],
    limit: int,
) -> int:
    ctid: ColumnClause[Any] = literal_column("ctid")
    batch = select(ctid).select_from(model).where(condition).limit(limit)
    result = await session.execute(update(model).where(ctid.in_(batch)).values(**values))
    return cast(CursorResult[Any], result).rowcount


async def tenant_places(session: AsyncSession) -> list[tuple[uuid.UUID, str]]:
    """Every tenant with its platform, for sweeps that apply a per-tenant policy."""
    rows = await session.execute(select(Tenant.id, Tenant.platform).order_by(Tenant.id))
    return [(row[0], row[1]) for row in rows]


async def delete_web_audit(session: AsyncSession, *, before: datetime, limit: int) -> int:
    _check(before)
    return await _delete_batch(session, WebAudit, WebAudit.at < before, limit)


async def delete_channel_changes(session: AsyncSession, *, before: datetime, limit: int) -> int:
    _check(before)
    return await _delete_batch(session, ChannelChange, ChannelChange.at < before, limit)


async def delete_turn_outcomes(session: AsyncSession, *, before: datetime, limit: int) -> int:
    _check(before)
    return await _delete_batch(session, TurnOutcome, TurnOutcome.started_at < before, limit)


async def delete_task_continuations(session: AsyncSession, *, before: datetime, limit: int) -> int:
    """Closed continuations, aged from when they closed."""
    _check(before)
    closed_at = func.coalesce(
        TaskContinuation.delivered_at, TaskContinuation.claimed_at, TaskContinuation.created_at
    )
    condition = and_(TaskContinuation.status.in_(_CLOSED_CONTINUATIONS), closed_at < before)
    return await _delete_batch(session, TaskContinuation, condition, limit)


async def delete_long_jobs(session: AsyncSession, *, before: datetime, limit: int) -> int:
    """Finished background jobs, with their dependent rows by cascade."""
    _check(before)
    condition = and_(
        LongJobRecord.state.in_(_FINISHED_JOB_STATES), LongJobRecord.finished_at < before
    )
    return await _delete_batch(session, LongJobRecord, condition, limit)


async def clear_compute_output(session: AsyncSession, *, before: datetime, limit: int) -> int:
    """Blank the output kept from finished compute tasks."""
    _check(before)
    condition = and_(ComputeTaskRecord.finished_at < before, ComputeTaskRecord.output_tail != "")
    return await _update_batch(session, ComputeTaskRecord, condition, {"output_tail": ""}, limit)


async def clear_routine_results(session: AsyncSession, *, before: datetime, limit: int) -> int:
    """Blank a routine's last result once its last run is old."""
    _check(before)
    condition = and_(Routine.last_finished_at < before, Routine.last_result_tail.is_not(None))
    return await _update_batch(session, Routine, condition, {"last_result_tail": None}, limit)


async def delete_credential_requests(session: AsyncSession, *, before: datetime, limit: int) -> int:
    """Credential requests used or expired before the cutoff.

    A request whose MCP sign-in completed stays: its flow cascades from it, and a
    completed flow is the lasting record of who signed in to which server.
    """
    _check(before)
    signed_in = exists().where(
        McpOAuthFlow.request_token == CredentialRequest.token,
        McpOAuthFlow.completed_at.is_not(None),
    )
    condition = and_(
        or_(
            CredentialRequest.used_at < before,
            and_(CredentialRequest.used_at.is_(None), CredentialRequest.expires_at < before),
        ),
        ~signed_in,
    )
    return await _delete_batch(session, CredentialRequest, condition, limit)


async def delete_github_access_requests(
    session: AsyncSession, *, before: datetime, limit: int
) -> int:
    """Access requests closed, or past their expiry, before the cutoff."""
    _check(before)
    condition = or_(
        and_(
            GitHubAccessRequest.status.in_(_CLOSED_ACCESS_REQUESTS),
            GitHubAccessRequest.updated_at < before,
        ),
        GitHubAccessRequest.expires_at < before,
    )
    return await _delete_batch(session, GitHubAccessRequest, condition, limit)


async def delete_deleted_web_threads(session: AsyncSession, *, before: datetime, limit: int) -> int:
    """Web threads deleted before the cutoff, with their messages by cascade."""
    _check(before)
    return await _delete_batch(session, WebThread, WebThread.deleted_at < before, limit)


async def delete_feedback(session: AsyncSession, *, before: datetime, limit: int) -> int:
    _check(before)
    return await _delete_batch(session, MessageFeedback, MessageFeedback.created_at < before, limit)


async def delete_support_escalations(session: AsyncSession, *, before: datetime, limit: int) -> int:
    _check(before)
    condition = SupportEscalation.created_at < before
    return await _delete_batch(session, SupportEscalation, condition, limit)


async def delete_channel_names(session: AsyncSession, *, before: datetime, limit: int) -> int:
    """Channel names not seen since the cutoff."""
    _check(before)
    seen = func.coalesce(PlatformChannelName.last_seen_at, PlatformChannelName.updated_at)
    return await _delete_batch(session, PlatformChannelName, seen < before, limit)


def _same_thread(model: type[TaskContinuation] | type[Watch]) -> ColumnElement[bool]:
    return and_(
        model.tenant_id == ThreadSession.tenant_id,
        model.platform == ThreadSession.platform,
        model.thread_id == ThreadSession.thread_id,
    )


async def delete_idle_thread_sessions(
    session: AsyncSession, *, tenant_id: uuid.UUID, before: datetime, limit: int
) -> int:
    """A tenant's thread mappings with no turn since the cutoff and none running.

    A thread that still has work aimed at it stays: a pending continuation, a
    live watch, or an enabled routine that posts into it would otherwise wake
    in a fresh session with none of its context. The thread's next message
    starts a fresh session.
    """
    _check(before)
    continuation = exists().where(
        _same_thread(TaskContinuation), TaskContinuation.status.in_(_OPEN_CONTINUATIONS)
    )
    watch = exists().where(_same_thread(Watch), Watch.status.in_(_LIVE_WATCHES))
    routine = exists().where(
        Routine.tenant_id == ThreadSession.tenant_id,
        Routine.enabled.is_(True),
        Routine.destination_kind == "thread",
        or_(
            Routine.destination_id == ThreadSession.thread_id,
            # Slack names a routine's thread `channel:ts`.
            Routine.destination_id.endswith(func.concat(":", ThreadSession.thread_id)),
        ),
    )
    condition = and_(
        ThreadSession.tenant_id == tenant_id,
        ThreadSession.last_turn_at < before,
        ThreadSession.active_turn_message_id.is_(None),
        ~continuation,
        ~watch,
        ~routine,
    )
    return await _delete_batch(session, ThreadSession, condition, limit)


async def delete_unreferenced_logins(session: AsyncSession, *, before: datetime, limit: int) -> int:
    """Sign-in records made before the cutoff that no web session uses and whose
    provider accounts no longer match any workspace identity."""
    _check(before)
    in_session = exists().where(WebSession.login_id == WebLogin.id)
    linked = (
        exists()
        .where(
            WebLoginAccount.login_id == WebLogin.id,
            WebIdentity.provider == WebLoginAccount.provider,
            WebIdentity.subject == WebLoginAccount.subject,
        )
        .correlate(WebLogin)
    )
    condition = and_(WebLogin.created_at < before, ~in_session, ~linked)
    return await _delete_batch(session, WebLogin, condition, limit)
