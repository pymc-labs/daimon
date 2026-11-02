"""Recorded channel setting changes: one row per change, written in the change's transaction."""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import ChannelChange
from daimon.core.stores.domain import ChangeSurface, ChannelChangeRow, ChannelSetting
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def record_change(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    setting: ChannelSetting,
    before: JsonValue,
    after: JsonValue,
    actor_account_id: uuid.UUID | None,
    surface: ChangeSurface,
    at: datetime | None = None,
) -> None:
    """Add one change row; a no-op change (same before and after) records nothing."""
    if before == after:
        return
    row = ChannelChange(
        tenant_id=tenant_id,
        platform=platform,
        channel_id=channel_id,
        setting=setting,
        before=before,
        after=after,
        actor_account_id=actor_account_id,
        surface=surface,
    )
    if at is not None:
        row.at = at
    session.add(row)
    await session.flush()


async def list_changes(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    limit: int = 200,
) -> list[ChannelChangeRow]:
    """The channel's changes, newest first."""
    rows = await session.scalars(
        select(ChannelChange)
        .where(ChannelChange.tenant_id == tenant_id, ChannelChange.channel_id == channel_id)
        .order_by(ChannelChange.at.desc(), ChannelChange.id.desc())
        .limit(limit)
    )
    return [ChannelChangeRow.model_validate(row) for row in rows]
