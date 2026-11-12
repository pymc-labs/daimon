"""This install's own id, for places no cloud names it (see `DeploymentIdentity`)."""

from __future__ import annotations

import uuid

from daimon.core._models import DeploymentIdentity
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def get_or_stamp_deployment_id(session: AsyncSession) -> uuid.UUID:
    """The stored id; the first caller stamps it and every later caller reads the same one."""
    stamped = await session.scalar(select(DeploymentIdentity.id))
    if stamped is not None:
        return stamped
    await session.execute(
        insert(DeploymentIdentity).values(singleton=True, id=uuid.uuid4()).on_conflict_do_nothing()
    )
    stamped = await session.scalar(select(DeploymentIdentity.id))
    if stamped is None:  # the insert above or another writer's made the row
        raise RuntimeError("deployment identity row missing after stamping")
    return stamped


async def reset_deployment_id(session: AsyncSession) -> uuid.UUID:
    """Replace the stored id, so a restored copy stops sharing the original's names."""
    fresh = uuid.uuid4()
    await get_or_stamp_deployment_id(session)
    await session.execute(update(DeploymentIdentity).values(id=fresh))
    return fresh
