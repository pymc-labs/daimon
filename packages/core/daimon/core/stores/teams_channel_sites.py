"""Teams channels' Files folders on granted SharePoint sites, one row per channel."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.core._models import TeamsChannelSite
from daimon.core.stores.domain import TeamsChannelSiteRow
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_teams_channel_site(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    group_id: str,
    site_id: str,
    drive_id: str,
    folder_id: str,
) -> None:
    now = datetime.now(UTC)
    found = {"group_id": group_id, "site_id": site_id, "drive_id": drive_id, "folder_id": folder_id}
    stmt = pg_insert(TeamsChannelSite).values(
        tenant_id=tenant_id, channel_id=channel_id, updated_at=now, **found
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[TeamsChannelSite.tenant_id, TeamsChannelSite.channel_id],
            set_={**found, "updated_at": now},
        )
    )


async def get_teams_channel_site(
    session: AsyncSession, *, tenant_id: uuid.UUID, channel_id: str
) -> TeamsChannelSiteRow | None:
    row = await session.get(TeamsChannelSite, (tenant_id, channel_id))
    return None if row is None else TeamsChannelSiteRow.model_validate(row)
