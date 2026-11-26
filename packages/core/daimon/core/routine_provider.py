"""The provider a routine's fire runs on, read from local facts only."""

from __future__ import annotations

from daimon.core.channel_backend import (
    agent_native_provider,
    channel_provider,
    effective_thread_backend,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.routine_delivery import delivery_target
from daimon.core.rule_views import routine_destination_channel
from daimon.core.stores.domain import RoutineRow
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import Provider
from sqlalchemy.ext.asyncio import AsyncSession


async def routine_provider(session: AsyncSession, row: RoutineRow, *, platform: str) -> Provider:
    """Its thread destination's retained provider, else a channel's, else its agent's.

    The scheduler routes the fire by it and a sandbox opt-in pins it, so a change of
    provider needs a new opt-in.
    """
    target = delivery_target(row, platform=platform)
    parent_channel = routine_destination_channel(row)
    provider: Provider | None = None
    if row.destination_kind == "thread" and parent_channel is not None:
        provider = await effective_thread_backend(
            session,
            tenant_id=row.tenant_id,
            platform=platform,
            channel_id=parent_channel,
            thread_id=routine_thread_id(row, platform=platform),
            caller_id=row.created_by_user_id,
            # A revision the thread kept for another agent is not this routine's, as in admission.
            agent_id=derive_agent_uuid(tenant_id=row.tenant_id, ma_agent_id=row.agent_id)
            if row.agent_id
            else None,
        )
    channels = {
        channel
        for channel in (
            row.channel_id,
            parent_channel,
            target.channel_id if target is not None else None,
        )
        if channel is not None
    }
    for channel in channels:
        if provider in (None, DEFAULT_BACKEND):
            provider = await channel_provider(
                session, tenant_id=row.tenant_id, platform=platform, channel_id=channel
            )
    if provider in (None, DEFAULT_BACKEND):
        provider = await agent_native_provider(
            session, tenant_id=row.tenant_id, agent_id=row.agent_id, agent_name=row.agent_name
        )
    return provider or DEFAULT_BACKEND


def routine_thread_id(row: RoutineRow, *, platform: str) -> str | None:
    """A thread destination's own thread id (Slack's thread ts), else None."""
    if row.destination_kind != "thread":
        return None
    target = delivery_target(row, platform=platform)
    return target.thread_ts if platform == "slack" and target is not None else row.destination_id
