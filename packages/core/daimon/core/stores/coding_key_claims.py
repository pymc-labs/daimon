"""Coding key claim codes: single-use codes a coding agent trades for a coding key.

- insert_claims: add a batch of codes, one per coding tool; only hashes are stored.
- find_claim_by_hash: the code a hash belongs to, read without a lock.
- lock_batch: row-lock every code of a batch in id order, so two claims in one
  batch take the locks in the same order and the second waits for the first.
- mark_claimed / kill_batch: a claimed code carries its key's jti; a killed one
  (a sibling's claim, a newer batch) is only marked used.
- kill_unused_batch / batch_claims: the issuer's own batch, to replace it and to read it.
- delete_claims_for_account / count_claims_for_account: the purge and its preview.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from daimon.core._models import CodingKeyClaim
from daimon.core.stores.domain import CodingKeyClaimRow
from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def insert_claims(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    agent_name: str,
    batch: uuid.UUID,
    created_at: datetime,
    expires_at: datetime,
    code_hashes: dict[str, str],
) -> None:
    """Insert one unused code per client; `code_hashes` maps a client id to its code's hash."""
    session.add_all(
        CodingKeyClaim(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            account_id=account_id,
            agent_id=agent_id,
            agent_name=agent_name,
            client=client,
            batch=batch,
            code_hash=code_hash,
            created_at=created_at,
            expires_at=expires_at,
        )
        for client, code_hash in code_hashes.items()
    )
    await session.flush()


async def find_claim_by_hash(session: AsyncSession, *, code_hash: str) -> CodingKeyClaimRow | None:
    """The code with this hash as it reads now, without a lock; None for an unknown code."""
    orm = await session.scalar(select(CodingKeyClaim).where(CodingKeyClaim.code_hash == code_hash))
    return None if orm is None else CodingKeyClaimRow.model_validate(orm)


async def lock_batch(session: AsyncSession, *, batch: uuid.UUID) -> list[CodingKeyClaimRow]:
    """Every code of the batch, row-locked in id order, as they stand once the lock is won."""
    stmt = (
        select(CodingKeyClaim)
        .where(CodingKeyClaim.batch == batch)
        .order_by(CodingKeyClaim.id)
        .with_for_update()
    )
    return [CodingKeyClaimRow.model_validate(orm) for orm in (await session.scalars(stmt)).all()]


async def mark_claimed(
    session: AsyncSession, *, claim_id: uuid.UUID, jti: uuid.UUID, now: datetime
) -> None:
    """Record that a code made the key `jti`. Call under `lock_batch`."""
    await session.execute(
        update(CodingKeyClaim).where(CodingKeyClaim.id == claim_id).values(used_at=now, jti=jti)
    )


async def kill_batch(
    session: AsyncSession, *, batch: uuid.UUID, now: datetime, except_id: uuid.UUID | None = None
) -> None:
    """Mark every unused code of the batch used, so none of them claims a key any more."""
    stmt = update(CodingKeyClaim).where(
        CodingKeyClaim.batch == batch, CodingKeyClaim.used_at.is_(None)
    )
    if except_id is not None:
        stmt = stmt.where(CodingKeyClaim.id != except_id)
    await session.execute(stmt.values(used_at=now))


async def kill_unused_batch(
    session: AsyncSession,
    *,
    batch: uuid.UUID,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    now: datetime,
) -> None:
    """Kill the unused codes of the issuer's own batch for this agent; any other batch is left."""
    await session.execute(
        update(CodingKeyClaim)
        .where(
            CodingKeyClaim.batch == batch,
            CodingKeyClaim.tenant_id == tenant_id,
            CodingKeyClaim.account_id == account_id,
            CodingKeyClaim.agent_id == agent_id,
            CodingKeyClaim.used_at.is_(None),
        )
        .values(used_at=now)
    )


async def batch_claims(
    session: AsyncSession,
    *,
    batch: uuid.UUID,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> Sequence[CodingKeyClaimRow]:
    """The batch's codes, only when this account issued it for this agent; else none."""
    stmt = select(CodingKeyClaim).where(
        CodingKeyClaim.batch == batch,
        CodingKeyClaim.tenant_id == tenant_id,
        CodingKeyClaim.account_id == account_id,
        CodingKeyClaim.agent_id == agent_id,
    )
    return [CodingKeyClaimRow.model_validate(orm) for orm in (await session.scalars(stmt)).all()]


async def delete_claims_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    """Hard-delete every code of an account. Idempotent; returns rows deleted."""
    result = await session.execute(
        delete(CodingKeyClaim).where(CodingKeyClaim.account_id == account_id)
    )
    await session.flush()
    return cast(CursorResult[Any], result).rowcount


async def count_claims_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    """Read-only count of the account's codes, for the purge preview."""
    result = await session.execute(
        select(func.count())
        .select_from(CodingKeyClaim)
        .where(CodingKeyClaim.account_id == account_id)
    )
    return result.scalar_one()
