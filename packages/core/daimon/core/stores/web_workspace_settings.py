"""Store for web_workspace_settings: a workspace's web app choices.

Which modules are off is the workspace tier of the `web.<module>` features, not a column
here (`modules_off` on the table is read by nothing); this store only reports it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import WebWorkspaceSettings
from daimon.core.features.registry import FEATURES, WEB_MODULES
from daimon.core.features.snapshot import FeaturePlace, resolve_one
from daimon.core.stores.domain import WebWorkspaceSettingsRow
from daimon.core.stores.feature_values import place_values
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def modules_off(session: AsyncSession, *, tenant_id: uuid.UUID) -> tuple[str, ...]:
    """The web modules a workspace has switched off."""
    place = FeaturePlace(tenant_id, "web")
    keys = [f"web.{module}" for module in WEB_MODULES]
    rows = await place_values(
        session, tenant_id=tenant_id, agent_id=None, channel_id=None, thread_key=None, keys=keys
    )
    return tuple(
        module
        for module, key in zip(WEB_MODULES, keys, strict=True)
        if not resolve_one(FEATURES[key], None, place, rows).on
    )


async def get_settings(session: AsyncSession, *, tenant_id: uuid.UUID) -> WebWorkspaceSettingsRow:
    """The workspace's choices; everything on when it made none."""
    orm = await session.scalar(
        select(WebWorkspaceSettings).where(WebWorkspaceSettings.tenant_id == tenant_id)
    )
    row = WebWorkspaceSettingsRow(tenant_id=tenant_id, default_agent=orm and orm.default_agent)
    return row.model_copy(update={"modules_off": await modules_off(session, tenant_id=tenant_id)})


async def save_settings(
    session: AsyncSession, *, tenant_id: uuid.UUID, default_agent: str | None, now: datetime
) -> WebWorkspaceSettingsRow:
    values = {"default_agent": default_agent, "updated_at": now}
    orm = await session.scalar(
        insert(WebWorkspaceSettings)
        .values(tenant_id=tenant_id, **values)
        .on_conflict_do_update(index_elements=[WebWorkspaceSettings.tenant_id], set_=values)
        .returning(WebWorkspaceSettings)
    )
    assert orm is not None, "an upsert returns its row"
    row = WebWorkspaceSettingsRow.model_validate(orm)
    return row.model_copy(update={"modules_off": await modules_off(session, tenant_id=tenant_id)})
