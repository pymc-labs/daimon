"""Tenant-fenced compute records. Row locks serialize machine actions and deletion."""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import Account, ComputeSessionRecord, Tenant
from daimon.core.compute_models import ComputeSession
from daimon.core.errors import StoreError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def scope(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    await db.execute(select(func.set_config("daimon.tenant_id", str(tenant_id), True)))


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
    db: AsyncSession, tenant_id: uuid.UUID, *, owner: uuid.UUID | None = None
) -> list[ComputeSession]:
    await scope(db, tenant_id)
    query = select(ComputeSessionRecord).where(ComputeSessionRecord.tenant_id == tenant_id)
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
        )
    )


async def warn(db: AsyncSession, session: ComputeSession, now: datetime) -> None:
    """Queue one expiry notice atomically with its marker; no completion job is involved."""
    from daimon.core.stores.task_continuations import record_continuation

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
        requested_work=f"Tell the owner: Fly session {session.id} expires at "
        f"{session.expires_at.isoformat()} and its files will be deleted. "
        "Ask them to extend it in an attended conversation if needed. Do not run commands.",
    )
    await save(db, session.model_copy(update={"warned_for": session.expires_at}))
