"""Whether "ask a person" is offered where a button, reaction or form is used.

The setting resolves at the agent that wrote the answer, so one agent can switch it off while
another in the same workspace keeps it. Every re-check on every platform asks here; none reads
the workspace alone. A thread with no session row at all (a reply that opened no thread) has no
agent, so the workspace decides, as before. A lookup that fails offers nothing.
"""

from __future__ import annotations

import uuid

import structlog
from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, place_feature
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.agent_posts import get_post
from daimon.core.stores.thread_sessions import list_thread_agents
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()


async def support_offered(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str | None,
    post: tuple[str, str] | None = None,
) -> bool:
    """`support_escalation` in effect for the agent that wrote the answer.

    `post` is the (channel id, message id) of the answer when known: its recorded author
    decides. Else the newest session row of the thread, of any status, decides."""
    agent_id: str | None = None
    if thread_id:
        try:
            async with sessions() as session:
                agents = await list_thread_agents(
                    session, tenant_id=tenant_id, platform=platform, thread_id=thread_id
                )
                recorded = (
                    await get_post(
                        session,
                        tenant_id=tenant_id,
                        platform=platform,
                        channel_id=post[0],
                        message_id=post[1],
                    )
                    if post is not None
                    else None
                )
        except Exception:
            log.warning("support.agent_lookup_failed", platform=platform, exc_info=True)
            return False
        if recorded is not None:
            agent_id = next(
                (
                    a
                    for a in agents
                    if a is not None
                    and derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=a) == recorded.agent_id
                ),
                None,
            )
            if agent_id is None:
                return False  # the author is not one of the thread's agents
        elif agents:
            agent_id = agents[0]
    place = FeaturePlace.of(tenant_id, platform, None, agent_id=agent_id)
    return (await place_feature(sessions, settings, place, "support_escalation")).on
