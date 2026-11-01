"""Store for web_workspace_settings: a workspace's web app choices."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from daimon.core._models import WebWorkspaceSettings
from daimon.core.stores.domain import WebWorkspaceSettingsRow
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def get_settings(session: AsyncSession, *, tenant_id: uuid.UUID) -> WebWorkspaceSettingsRow:
    """The workspace's choices; everything on when it made none."""
    orm = await session.scalar(
        select(WebWorkspaceSettings).where(WebWorkspaceSettings.tenant_id == tenant_id)
    )
    if orm is None:
        return WebWorkspaceSettingsRow(tenant_id=tenant_id)
    return WebWorkspaceSettingsRow.model_validate(orm)


async def save_settings(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    modules_off: Sequence[str],
    signin_off: Sequence[str],
    default_agent: str | None,
    now: datetime,
) -> WebWorkspaceSettingsRow:
    values = {
        "modules_off": sorted(set(modules_off)),
        "signin_off": sorted(set(signin_off)),
        "default_agent": default_agent,
        "updated_at": now,
    }
    orm = await session.scalar(
        insert(WebWorkspaceSettings)
        .values(tenant_id=tenant_id, **values)
        .on_conflict_do_update(index_elements=[WebWorkspaceSettings.tenant_id], set_=values)
        .returning(WebWorkspaceSettings)
    )
    assert orm is not None, "an upsert returns its row"
    return WebWorkspaceSettingsRow.model_validate(orm)
