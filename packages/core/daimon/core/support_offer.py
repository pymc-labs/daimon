"""Whether "ask a person" is offered where a button, reaction or form is used.

The setting resolves at the agent of the conversation, so one agent can switch it off while
another in the same workspace keeps it. Every re-check on every platform asks here; none reads
the workspace alone. A place with no live session (a reply that opened no thread) has no agent,
so the workspace decides, as before.
"""

from __future__ import annotations

import uuid

import structlog
from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, place_feature
from daimon.core.stores.thread_sessions import get_latest_thread_session
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()


async def support_offered(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str | None,
) -> bool:
    """`support_escalation` in effect for the agent of `thread_id`'s newest live session."""
    agent_id: str | None = None
    if thread_id:
        try:
            async with sessions() as session:
                row = await get_latest_thread_session(
                    session, tenant_id=tenant_id, platform=platform, thread_id=thread_id
                )
            agent_id = row.ma_agent_id if row is not None else None
        except Exception:
            log.warning("support.agent_lookup_failed", platform=platform, exc_info=True)
    place = FeaturePlace.of(tenant_id, platform, None, agent_id=agent_id)
    return (await place_feature(sessions, settings, place, "support_escalation")).on
