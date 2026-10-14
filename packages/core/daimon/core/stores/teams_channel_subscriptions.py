"""Graph subscriptions to Teams private and shared channels, one row per channel."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from daimon.core._models import TeamsChannelSubscription
from daimon.core.stores.domain import TeamsChannelSubscriptionRow
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_teams_channel_subscription(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    team_id: str,
    group_id: str,
    channel_type: str,
    channel_name: str | None,
    team_name: str | None,
    bot_name: str | None,
    service_url: str | None,
    subscription_id: str,
    client_state_sha256: bytes,
    expires_at: datetime,
) -> None:
    """Record a new subscription; a channel's earlier one, and its watermark, are kept apart."""
    now = datetime.now(UTC)
    values = {
        "team_id": team_id,
        "group_id": group_id,
        "channel_type": channel_type,
        "channel_name": channel_name,
        "team_name": team_name,
        "bot_name": bot_name,
        "service_url": service_url,
        "subscription_id": subscription_id,
        "client_state_sha256": client_state_sha256,
        "expires_at": expires_at,
    }
    stmt = pg_insert(TeamsChannelSubscription).values(
        tenant_id=tenant_id, channel_id=channel_id, updated_at=now, **values
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[
                TeamsChannelSubscription.tenant_id,
                TeamsChannelSubscription.channel_id,
            ],
            set_={**values, "updated_at": now},
        )
    )


async def get_teams_channel_subscription(
    session: AsyncSession, *, tenant_id: uuid.UUID, channel_id: str
) -> TeamsChannelSubscriptionRow | None:
    row = await session.get(TeamsChannelSubscription, (tenant_id, channel_id))
    return None if row is None else TeamsChannelSubscriptionRow.model_validate(row)


async def list_teams_channel_subscriptions(
    session: AsyncSession, *, tenant_id: uuid.UUID, subscription_ids: Sequence[str] | None = None
) -> list[TeamsChannelSubscriptionRow]:
    """Every subscription of the tenant, or only those with `subscription_ids`."""
    query = select(TeamsChannelSubscription).where(TeamsChannelSubscription.tenant_id == tenant_id)
    if subscription_ids is not None:
        query = query.where(TeamsChannelSubscription.subscription_id.in_(subscription_ids))
    rows = await session.scalars(query.order_by(TeamsChannelSubscription.expires_at))
    return [TeamsChannelSubscriptionRow.model_validate(row) for row in rows]


async def update_teams_channel_subscription(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    subscription_id: str,
    expires_at: datetime,
    client_state_sha256: bytes | None = None,
) -> None:
    """A renewal's new expiry, or with `client_state_sha256` a replacement subscription."""
    values: dict[str, object] = {
        "subscription_id": subscription_id,
        "expires_at": expires_at,
        "updated_at": datetime.now(UTC),
    }
    if client_state_sha256 is not None:
        values["client_state_sha256"] = client_state_sha256
    await session.execute(
        update(TeamsChannelSubscription)
        .where(
            TeamsChannelSubscription.tenant_id == tenant_id,
            TeamsChannelSubscription.channel_id == channel_id,
        )
        .values(**values)
    )


async def record_teams_channel_watermark(
    session: AsyncSession, *, tenant_id: uuid.UUID, channel_id: str, message_id: str, at: datetime
) -> None:
    """Move the newest-message mark to `at`; an older message leaves it where it is."""
    model = TeamsChannelSubscription
    await session.execute(
        update(model)
        .where(
            model.tenant_id == tenant_id,
            model.channel_id == channel_id,
            func.coalesce(model.last_message_at, at) <= at,
        )
        .values(last_message_id=message_id, last_message_at=at)
    )


async def delete_teams_channel_subscriptions(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str | None = None,
    team_id: str | None = None,
) -> list[str]:
    """Delete one channel's row or a whole team's; the Graph subscription ids they held."""
    model = TeamsChannelSubscription
    stmt = delete(model).where(model.tenant_id == tenant_id)
    if channel_id is not None:
        stmt = stmt.where(model.channel_id == channel_id)
    if team_id is not None:
        stmt = stmt.where(model.team_id == team_id)
    if channel_id is None and team_id is None:
        raise ValueError("name a channel or a team")
    deleted = await session.scalars(stmt.returning(model.subscription_id))
    return list(deleted)
