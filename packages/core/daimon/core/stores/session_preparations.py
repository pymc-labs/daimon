"""Resume points for moving a caller's task onto a new configuration.

Replacing a session is not one write: it is a billed checkpoint turn, a file
upload, a `sessions.create` and finally a supersede. A process that dies
between any two of those must not repeat the billed part, so each step records
how far it got. `(mapping_id, target_fingerprint)` is unique, which makes the
row the natural resume key: retrying the *same* target finds the same row, and
a target that moved under us starts a fresh one instead of resuming into a
configuration nobody asked for.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime

from daimon.core._models import SessionPreparation
from daimon.core.stores.domain import PreparationStage, SessionPreparationRow, TransferKind
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_preparation(
    session: AsyncSession,
    *,
    mapping_id: _uuid.UUID,
    target_fingerprint: str,
) -> SessionPreparationRow:
    """Claim the preparation for this (mapping, target), creating it if new.

    One statement, so two processes racing the same replacement end up on one
    row rather than one row each. An existing row comes back with `attempts`
    incremented: that counter is how a caller distinguishes a first try from a
    retry loop, which is what backoff after a `failed` stage reads.
    """
    statement = (
        insert(SessionPreparation)
        .values(
            mapping_id=mapping_id,
            target_fingerprint=target_fingerprint,
            stage="decided",
        )
        .on_conflict_do_update(
            constraint="uq_session_preparations_mapping_fingerprint",
            set_={"attempts": SessionPreparation.attempts + 1},
        )
        .returning(SessionPreparation)
    )
    orm = (await session.execute(statement)).scalar_one()
    await session.flush()
    return SessionPreparationRow.model_validate(orm)


async def get_preparation(
    session: AsyncSession,
    *,
    mapping_id: _uuid.UUID,
    target_fingerprint: str,
) -> SessionPreparationRow | None:
    orm = (
        await session.execute(
            select(SessionPreparation).where(
                SessionPreparation.mapping_id == mapping_id,
                SessionPreparation.target_fingerprint == target_fingerprint,
            )
        )
    ).scalar_one_or_none()
    return None if orm is None else SessionPreparationRow.model_validate(orm)


async def advance_stage(
    session: AsyncSession,
    *,
    id: _uuid.UUID,
    stage: PreparationStage,
    now: datetime,
    transfer_file_id: str | None = None,
    transfer_kind: TransferKind | None = None,
    new_mapping_id: _uuid.UUID | None = None,
) -> None:
    """Move a preparation forward, carrying whatever that step produced.

    The three optional fields are written only when supplied: a later stage
    must not erase the transfer the `uploaded` stage recorded just because it
    has nothing new to say about it.
    """
    values: dict[str, object] = {"stage": stage, "updated_at": now}
    if transfer_file_id is not None:
        values["transfer_file_id"] = transfer_file_id
    if transfer_kind is not None:
        values["transfer_kind"] = transfer_kind
    if new_mapping_id is not None:
        values["new_mapping_id"] = new_mapping_id
    await session.execute(
        update(SessionPreparation).where(SessionPreparation.id == id).values(**values)
    )
    await session.flush()


async def fail_preparation(
    session: AsyncSession,
    *,
    id: _uuid.UUID,
    reason: str,
    now: datetime,
) -> None:
    """Record why a preparation stopped, keeping everything it already produced.

    The row survives so the next attempt can see it failed and back off, and so
    the caller can be told what was preserved rather than guessing.
    """
    await session.execute(
        update(SessionPreparation)
        .where(SessionPreparation.id == id)
        .values(stage="failed", failure_reason=reason, updated_at=now)
    )
    await session.flush()


async def delete_preparation(session: AsyncSession, *, id: _uuid.UUID) -> None:
    """Drop a finished preparation so the same target can be prepared again."""
    await session.execute(delete(SessionPreparation).where(SessionPreparation.id == id))
    await session.flush()
