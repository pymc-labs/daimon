"""Recorded channel setting changes: one row per change, written in the change's transaction."""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import ChannelChange
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores.domain import ChangeSurface, ChannelChangeRow, ChannelSetting
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields, unset_fields
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


async def write_channel_default(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    agent_name: str | None,
    actor_account_id: uuid.UUID | None,
    surface: ChangeSurface,
    set_by_admin: bool = False,
) -> str | None:
    """Set (None: clear) the channel's own default agent and record it; return the prior one.

    Every surface that changes a channel's default writes through here, so each change
    is recorded. The caller has authorized it and holds the transaction.
    """
    scope = ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id)
    prior_row = await get_scope(session, scope=scope)
    prior = prior_row.agent_name if prior_row is not None else None
    if agent_name is None:
        if prior is not None:
            await unset_fields(
                session, scope=scope, fields=["agent_name"], actor_account_id=actor_account_id
            )
    else:
        await set_fields(
            session,
            scope=scope,
            tenant_id=tenant_id,
            agent_name=agent_name,
            mode="agent",
            actor_account_id=actor_account_id,
            set_by_admin=set_by_admin,
        )
    await record_change(
        session,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=channel_id,
        setting="default_agent",
        before=prior,
        after=agent_name,
        actor_account_id=actor_account_id,
        surface=surface,
    )
    return prior
