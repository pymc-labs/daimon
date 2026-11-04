"""Per-turn reach tallies (`daimon.core.outbound.reach`), locked while a post is checked."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core._models import ReachTally as ReachTallyModel
from daimon.core.outbound.reach import ReachPlan, ReachTally
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

#: A turn never runs this long; older rows are dropped on the next write.
KEEP_FOR = timedelta(days=1)


async def lock_tally(
    session: AsyncSession, *, tenant_id: uuid.UUID, turn_key: str
) -> tuple[ReachTally, ReachPlan | None]:
    """The turn's tally and approved plan, row-locked until the transaction ends."""
    await session.execute(
        delete(ReachTallyModel).where(ReachTallyModel.updated_at < datetime.now(UTC) - KEEP_FOR)
    )
    await session.execute(
        insert(ReachTallyModel)
        .values(tenant_id=tenant_id, turn_key=turn_key)
        .on_conflict_do_nothing()
    )
    row = (
        await session.execute(
            select(ReachTallyModel)
            .where(ReachTallyModel.tenant_id == tenant_id, ReachTallyModel.turn_key == turn_key)
            .with_for_update()
        )
    ).scalar_one()
    tally = ReachTally(
        threads=row.threads,
        own_threads=row.own_threads,
        places=frozenset(row.places),
        people=frozenset(row.people),
    )
    approved = (
        None
        if row.approved_threads is None
        else ReachPlan(
            threads=row.approved_threads,
            places=row.approved_places or 0,
            people=row.approved_people or 0,
        )
    )
    return tally, approved


async def approved_people(session: AsyncSession, *, tenant_id: uuid.UUID, turn_key: str) -> int:
    """People the turn's requester approved tagging, or 0 where nothing was approved."""
    row = await session.get(ReachTallyModel, (tenant_id, turn_key))
    return (row.approved_people or 0) if row is not None else 0


async def save_tally(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    turn_key: str,
    tally: ReachTally | None = None,
    approved: ReachPlan | None = None,
) -> None:
    """Store what the turn reached, or the plan its requester approved, on the locked row."""
    row = await session.get(ReachTallyModel, (tenant_id, turn_key))
    if row is None:
        return
    if tally is not None:
        row.threads = tally.threads
        row.own_threads = tally.own_threads
        row.places = sorted(tally.places)
        row.people = sorted(tally.people)
    if approved is not None:
        row.approved_threads = approved.threads
        row.approved_places = approved.places
        row.approved_people = approved.people
    row.updated_at = datetime.now(UTC)
