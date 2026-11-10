"""Every channel id a tenant's settings name, read across the tables that carry one."""

from __future__ import annotations

import uuid

from daimon.core._models import (
    AgentToolRuleRecord,
    ChannelBudget,
    ChannelConfig,
    ChannelConfigRevision,
    ChannelSkill,
    FeatureValue,
    PlatformChannelName,
    Routine,
    SharedChannel,
    TeamsChannelSubscription,
)
from sqlalchemy import Select, and_, func, select, union
from sqlalchemy.ext.asyncio import AsyncSession


def _pinned_channels(tenant_id: uuid.UUID, platform: str) -> Select[tuple[str]]:
    """Channels whose latest backend revision is the channel's own (a provider pin)."""
    latest = (
        select(
            ChannelConfigRevision.channel_id,
            ChannelConfigRevision.origin,
            func.row_number()
            .over(
                partition_by=ChannelConfigRevision.channel_id,
                order_by=ChannelConfigRevision.local.desc(),
            )
            .label("n"),
        )
        .where(
            ChannelConfigRevision.tenant_id == tenant_id,
            ChannelConfigRevision.platform == platform,
        )
        .subquery()
    )
    return select(latest.c.channel_id).where(latest.c.n == 1, latest.c.origin == "channel")


async def list_configured_channel_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str
) -> set[str]:
    """Channel ids with a setting, a name or a subscription; the access policy is the caller's.

    Thread-scoped rows are left out; a routine counts in the channel it posts to.
    """
    selects: list[Select[tuple[str]] | Select[tuple[str | None]]] = [
        select(ChannelConfig.channel_id).where(ChannelConfig.tenant_id == tenant_id),
        select(ChannelBudget.channel_id).where(
            ChannelBudget.tenant_id == tenant_id, ChannelBudget.platform == platform
        ),
        select(ChannelSkill.channel_id).where(
            ChannelSkill.tenant_id == tenant_id, ChannelSkill.platform == platform
        ),
        select(AgentToolRuleRecord.channel_id).where(
            AgentToolRuleRecord.tenant_id == tenant_id, AgentToolRuleRecord.channel_id.is_not(None)
        ),
        select(FeatureValue.channel_id).where(
            FeatureValue.tenant_id == tenant_id,
            FeatureValue.tier == "channel",
            FeatureValue.agent_id == "",
        ),
        select(Routine.channel_id).where(
            Routine.tenant_id == tenant_id, Routine.channel_id.is_not(None)
        ),
        select(Routine.destination_id).where(
            and_(
                Routine.tenant_id == tenant_id,
                Routine.channel_id.is_(None),
                Routine.destination_kind == "channel",
            )
        ),
        _pinned_channels(tenant_id, platform),
        select(SharedChannel.channel_id).where(
            SharedChannel.tenant_id == tenant_id, SharedChannel.platform == platform
        ),
        # A deleted channel or a direct message is listed only for what else names it.
        select(PlatformChannelName.channel_id).where(
            PlatformChannelName.tenant_id == tenant_id,
            PlatformChannelName.platform == platform,
            PlatformChannelName.status == "named",
        ),
    ]
    if platform == "teams":
        selects.append(
            select(TeamsChannelSubscription.channel_id).where(
                TeamsChannelSubscription.tenant_id == tenant_id
            )
        )
    reads = union(*selects)
    return {channel_id for channel_id in (await session.execute(reads)).scalars() if channel_id}


async def list_shared_channel_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str
) -> set[str]:
    """Channels known to be shared with another organisation."""
    rows = await session.scalars(
        select(SharedChannel.channel_id).where(
            SharedChannel.tenant_id == tenant_id, SharedChannel.platform == platform
        )
    )
    return set(rows)
