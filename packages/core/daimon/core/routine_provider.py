"""The provider a routine's fire runs on, read from local facts only."""

from __future__ import annotations

from typing import TYPE_CHECKING

from daimon.core.channel_backend import (
    agent_native_provider,
    channel_ref,
    current_backend,
    kept_thread_backend,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.routine_delivery import delivery_target
from daimon.core.rule_views import routine_destination_channel
from daimon.core.stores import mux_state
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import Provider
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from daimon.core.fire.subject import RoutineFacts


async def routine_provider(session: AsyncSession, row: RoutineFacts, *, platform: str) -> Provider:
    """Its thread destination's retained provider, else a channel's, else its agent's.

    Read where admission admits the fire (`provider_routine.admit_provider_routine`): the
    destination's channel, else the one it was made in. A thread that kept Claude stays on
    it. The scheduler routes the fire by it and a sandbox opt-in pins it, so a change of
    provider needs a new opt-in.
    """
    channel = routine_destination_channel(row) or row.channel_id
    thread_id = routine_thread_id(row, platform=platform)
    if channel is None:
        provider = None
    elif thread_id is not None and (
        kept := await kept_thread_backend(
            session,
            tenant_id=row.tenant_id,
            platform=platform,
            channel_id=channel,
            thread_id=thread_id,
            caller_id=row.created_by_user_id,
            # A revision the thread kept for another agent is not this routine's, as in admission.
            agent_id=derive_agent_uuid(tenant_id=row.tenant_id, ma_agent_id=row.agent_id)
            if row.agent_id
            else None,
        )
    ):
        return kept
    else:
        place = channel_ref(row.tenant_id, platform, channel)
        pinned = await current_backend(session, place)
        # A cleared pin configures nothing, as admission reads it.
        cleared = pinned is not None and (
            await mux_state.config_origin(session, place, pinned.local) == "channel_clear"
        )
        provider = pinned.backend if pinned is not None and not cleared else None
    if provider is None:
        provider = await agent_native_provider(
            session, tenant_id=row.tenant_id, agent_id=row.agent_id, agent_name=row.agent_name
        )
    return provider or DEFAULT_BACKEND


def routine_thread_id(row: RoutineFacts, *, platform: str) -> str | None:
    """A thread destination's own thread id (Slack's thread ts), else None."""
    if row.destination_kind != "thread":
        return None
    target = delivery_target(row, platform=platform)
    return target.thread_ts if platform == "slack" and target is not None else row.destination_id
