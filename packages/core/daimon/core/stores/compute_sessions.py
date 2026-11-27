"""Tenant-fenced compute records. Row locks serialize machine actions and deletion."""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import Account, ComputeSessionRecord, Tenant
from daimon.core.access_policy import AGENT_CHAT_SCOPE_PREFIX
from daimon.core.compute_models import ComputeSession
from daimon.core.errors import StoreError
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.stores.session_settle_state import find_agent_chat_session
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def scope(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    await db.execute(select(func.set_config("daimon.tenant_id", str(tenant_id), True)))


def in_agent_chat(session: ComputeSession) -> bool:
    """Whether the machine belongs to an agent-chat session (`AGENT_CHAT_SCOPE_PREFIX`)."""
    return session.thread_id.startswith(AGENT_CHAT_SCOPE_PREFIX)


def notices_reach(session: ComputeSession) -> bool:
    """Whether a notice queued for the session's conversation can reach it: the one rule for
    every notice (a command's end, the expiry warning and a failed start). Not for an
    agent-chat session's machine: no poller serves its scope, so it checks again itself."""
    return not in_agent_chat(session)


async def feature_place(db: AsyncSession, session: ComputeSession) -> FeaturePlace | None:
    """Where a machine's features are read: the one place for every reader of a row
    (`ComputeSessions._allowed`, `compute_tasks.wake_runs_on`).

    A chat conversation's machine is read at its channel, thread and agent. An agent-chat
    session's machine has no channel or thread of its own: it is read at the channel and
    agent its session was recorded with (`find_agent_chat_session`, for the grant in its
    scope and its owner), never at its scope. None when that record is not found.
    """
    if not in_agent_chat(session):
        return FeaturePlace.of(
            session.tenant_id,
            session.platform,
            session.parent_channel_id,
            session.thread_id,
            agent_id=session.agent_id,
        )
    found = await find_agent_chat_session(
        db,
        grant=session.thread_id.removeprefix(AGENT_CHAT_SCOPE_PREFIX),
        account_id=session.owner_account_id,
    )
    if found is None:
        return None
    return FeaturePlace.of(
        session.tenant_id, session.platform, found.channel_id, agent_id=session.agent_id
    )


async def get(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    lock: bool = False,
    skip_locked: bool = False,
) -> ComputeSession | None:
    await scope(db, tenant_id)
    query = select(ComputeSessionRecord).where(
        ComputeSessionRecord.tenant_id == tenant_id, ComputeSessionRecord.id == session_id
    )
    if lock:
        query = query.with_for_update(skip_locked=skip_locked)
    row = await db.scalar(query)
    return None if row is None else ComputeSession.model_validate(row)


async def list_sessions(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    owner: uuid.UUID | None = None,
    include_deleted: bool = True,
) -> list[ComputeSession]:
    await scope(db, tenant_id)
    query = select(ComputeSessionRecord).where(ComputeSessionRecord.tenant_id == tenant_id)
    if not include_deleted:
        query = query.where(ComputeSessionRecord.state != "deleted")
    if owner is not None:
        query = query.where(ComputeSessionRecord.owner_account_id == owner)
    return [ComputeSession.model_validate(row) for row in await db.scalars(query)]


async def register(db: AsyncSession, session: ComputeSession, *, cap: int) -> ComputeSession:
    await scope(db, session.tenant_id)
    tenant = await db.scalar(
        select(Tenant.id)
        .where(Tenant.id == session.tenant_id, Tenant.archived_at.is_(None))
        .with_for_update()
    )
    account = await db.scalar(
        select(Account.id).where(
            Account.id == session.owner_account_id, Account.tenant_id == session.tenant_id
        )
    )
    if tenant is None or account is None:
        raise StoreError("The session owner is unavailable")
    existing = await db.scalar(
        select(ComputeSessionRecord).where(
            ComputeSessionRecord.tenant_id == session.tenant_id,
            ComputeSessionRecord.request_key == session.request_key,
        )
    )
    if existing is not None:
        if existing.owner_account_id != session.owner_account_id:
            raise StoreError("Session not found")
        return ComputeSession.model_validate(existing)
    count = await db.scalar(
        select(func.count())
        .select_from(ComputeSessionRecord)
        .where(
            ComputeSessionRecord.tenant_id == session.tenant_id,
            ComputeSessionRecord.owner_account_id == session.owner_account_id,
            ComputeSessionRecord.state != "deleted",
        )
    )
    if (count or 0) >= cap:
        raise StoreError("Your session limit is reached. Stop a session first.")
    db.add(ComputeSessionRecord(**session.model_dump()))
    await db.flush()
    return session


async def save(db: AsyncSession, session: ComputeSession) -> None:
    """Callers hold the row lock until their provider action and this write settle."""
    await scope(db, session.tenant_id)
    await db.execute(
        update(ComputeSessionRecord)
        .where(
            ComputeSessionRecord.tenant_id == session.tenant_id,
            ComputeSessionRecord.id == session.id,
        )
        .values(
            state=session.state,
            machine_id=session.machine_id,
            expires_at=session.expires_at,
            warned_for=session.warned_for,
            extension_keys=session.extension_keys,
            wake_turns=session.wake_turns,
        )
    )


async def warn(db: AsyncSession, session: ComputeSession, now: datetime) -> None:
    """Queue one expiry notice atomically with its marker; no completion job is involved.
    The marker is kept where no notice can reach (`notices_reach`)."""
    from daimon.core.stores.task_continuations import record_continuation

    if not notices_reach(session):
        await save(db, session.model_copy(update={"warned_for": session.expires_at}))
        return
    await record_continuation(
        db,
        tenant_id=session.tenant_id,
        platform=session.platform,
        parent_channel_id=session.parent_channel_id,
        thread_id=session.thread_id,
        requester_account_id=session.owner_account_id,
        requester_external_user_id=session.owner_external_user_id,
        target_ma_agent_id=session.agent_id,
        target_name=session.agent_name,
        reason="timer",
        idempotency_key=uuid.uuid5(session.id, session.expires_at.isoformat()),
        available_at=now,
        attended=False,
        requested_work=f"Tell the owner: machine {session.id} expires at "
        f"{session.expires_at.isoformat()} and its files will be deleted. "
        "Ask them to extend it in an attended conversation if needed. Do not run commands.",
    )
    await save(db, session.model_copy(update={"warned_for": session.expires_at}))


async def start_failed(db: AsyncSession, session: ComputeSession, now: datetime) -> None:
    """Queue the one notice that a machine never started, for an owner who is not in the call,
    where a notice can reach (`notices_reach`)."""
    from daimon.core.stores.task_continuations import record_continuation

    if not notices_reach(session):
        return
    await record_continuation(
        db,
        tenant_id=session.tenant_id,
        platform=session.platform,
        parent_channel_id=session.parent_channel_id,
        thread_id=session.thread_id,
        requester_account_id=session.owner_account_id,
        requester_external_user_id=session.owner_external_user_id,
        target_ma_agent_id=session.agent_id,
        target_name=session.agent_name,
        reason="timer",
        idempotency_key=uuid.uuid5(session.id, "start-failed"),
        available_at=now,
        attended=False,
        requested_work=f"Tell the owner: machine {session.id} could not start and is being "
        f"removed. "
        f"An admin can check the image profile {session.profile}. Do not run commands.",
    )
