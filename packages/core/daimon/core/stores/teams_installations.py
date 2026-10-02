"""Teams the bot is installed in: upserted from activities, deleted on removal."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.core._models import TeamsInstallation
from daimon.core.stores.domain import TeamsInstallationRow
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def record_teams_installation(
    session: AsyncSession, *, tenant_id: uuid.UUID, team_id: str, group_id: str, name: str | None
) -> bool:
    """Upsert one team; a missing `name` keeps the stored one. True if it was new."""
    now = datetime.now(UTC)
    stmt = pg_insert(TeamsInstallation).values(
        tenant_id=tenant_id,
        team_id=team_id,
        group_id=group_id,
        name=name,
        installed_at=now,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[TeamsInstallation.tenant_id, TeamsInstallation.team_id],
        set_={
            "group_id": group_id,
            "name": func.coalesce(stmt.excluded.name, TeamsInstallation.name),
            "updated_at": now,
        },
    ).returning(TeamsInstallation.installed_at)
    installed_at = (await session.execute(stmt)).scalar_one()
    return installed_at == now


async def list_teams_installations(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[TeamsInstallationRow]:
    rows = await session.scalars(
        select(TeamsInstallation)
        .where(TeamsInstallation.tenant_id == tenant_id)
        .order_by(TeamsInstallation.name, TeamsInstallation.team_id)
    )
    return [TeamsInstallationRow.model_validate(row) for row in rows]


async def get_teams_installation(
    session: AsyncSession, *, tenant_id: uuid.UUID, team_id: str
) -> TeamsInstallationRow | None:
    row = await session.get(TeamsInstallation, (tenant_id, team_id))
    return None if row is None else TeamsInstallationRow.model_validate(row)


async def delete_teams_installation(
    session: AsyncSession, *, tenant_id: uuid.UUID, team_id: str
) -> None:
    await session.execute(
        delete(TeamsInstallation).where(
            TeamsInstallation.tenant_id == tenant_id, TeamsInstallation.team_id == team_id
        )
    )
