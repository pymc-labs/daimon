"""Durable account-level Managed Agents erasure work."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.core._models import PrivacySessionDelete
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class PrivacySessionDeleteRow:
    account_id: uuid.UUID
    tenant_ids: tuple[uuid.UUID, ...]
    pending_session_ids: dict[str, list[str]]


def _row(model: PrivacySessionDelete) -> PrivacySessionDeleteRow:
    return PrivacySessionDeleteRow(
        model.account_id,
        tuple(uuid.UUID(value) for value in model.tenant_ids),
        model.pending_session_ids,
    )


async def ensure_work(
    session: AsyncSession, *, account_id: uuid.UUID, tenant_ids: set[uuid.UUID]
) -> None:
    """Called inside the local purge transaction, before account deletion."""
    row = await session.get(PrivacySessionDelete, account_id, with_for_update=True)
    if row is None:
        session.add(
            PrivacySessionDelete(
                account_id=account_id,
                tenant_ids=sorted(str(value) for value in tenant_ids),
                pending_session_ids={},
            )
        )
    else:
        row.tenant_ids = sorted(set(row.tenant_ids) | {str(value) for value in tenant_ids})
    await session.flush()


async def lock_account(session: AsyncSession, *, account_id: uuid.UUID) -> None:
    """Serialize local purge and retries for this account across processes."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:account_id, 0))"),
        {"account_id": str(account_id)},
    )


async def get_work(
    session: AsyncSession, *, account_id: uuid.UUID
) -> PrivacySessionDeleteRow | None:
    row = await session.get(PrivacySessionDelete, account_id)
    return _row(row) if row is not None else None


async def list_work(session: AsyncSession) -> list[PrivacySessionDeleteRow]:
    result = await session.execute(
        select(PrivacySessionDelete).order_by(PrivacySessionDelete.created_at)
    )
    return [_row(row) for row in result.scalars()]


async def add_pending(
    session: AsyncSession, *, account_id: uuid.UUID, tenant_id: uuid.UUID, session_ids: set[str]
) -> None:
    row = await session.get(PrivacySessionDelete, account_id, with_for_update=True)
    if row is None:
        return
    pending = dict(row.pending_session_ids)
    key = str(tenant_id)
    pending[key] = sorted(set(pending.get(key, [])) | session_ids)
    row.pending_session_ids = pending
    await session.flush()


async def remove_pending(
    session: AsyncSession, *, account_id: uuid.UUID, tenant_id: uuid.UUID, session_id: str
) -> None:
    row = await session.get(PrivacySessionDelete, account_id, with_for_update=True)
    if row is None:
        return
    pending = dict(row.pending_session_ids)
    key = str(tenant_id)
    pending[key] = sorted(set(pending.get(key, [])) - {session_id})
    row.pending_session_ids = pending
    await session.flush()


async def finish_work(session: AsyncSession, *, account_id: uuid.UUID) -> None:
    row = await session.get(PrivacySessionDelete, account_id, with_for_update=True)
    if row is not None and not any(row.pending_session_ids.values()):
        await session.execute(
            delete(PrivacySessionDelete).where(PrivacySessionDelete.account_id == account_id)
        )
