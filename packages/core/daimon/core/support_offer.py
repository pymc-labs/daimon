"""Whether "ask a person" is offered where a button, reaction or form is used.

The setting resolves at the agent that wrote the answer, so one agent can switch it off while
another in the same workspace keeps it. Every re-check on every platform asks here; none reads
the workspace alone. A lookup or read that fails offers nothing.
"""

from __future__ import annotations

import uuid

import structlog
from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, place_feature_or_none
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.agent_posts import get_post
from daimon.core.stores.feature_values import list_feature_values
from daimon.core.stores.thread_sessions import newest_thread_agent
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KEY = "support_escalation"
log = structlog.get_logger()


async def _answer_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    post: tuple[str, str],
) -> str | None:
    """The agent whose own value applies to the answer at `post`; None leaves the workspace.

    A recorded answer names its author, and only an agent with its own value can differ from
    the workspace, so the author is looked up among those. An answer with no record falls back
    to the newest session row of the thread, of any status."""
    recorded = await get_post(
        session, tenant_id=tenant_id, platform=platform, channel_id=post[0], message_id=post[1]
    )
    if recorded is None:
        return await newest_thread_agent(
            session, tenant_id=tenant_id, platform=platform, thread_id=thread_id
        )
    rows = await list_feature_values(session, tenant_id=tenant_id, keys=(KEY,), tier="agent")
    return next(
        (
            row.agent_id
            for row in rows
            if derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=row.agent_id) == recorded.agent_id
        ),
        None,
    )


async def support_offered(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    post: tuple[str, str],
) -> bool:
    """`support_escalation` in effect for the agent that wrote the answer at `post`, the
    (channel id, message id) it was posted as, in thread `thread_id`."""
    try:
        async with sessions() as session:
            agent_id = await _answer_agent(
                session, tenant_id=tenant_id, platform=platform, thread_id=thread_id, post=post
            )
    except SQLAlchemyError:
        log.warning("support.agent_lookup_failed", platform=platform, exc_info=True)
        return False
    place = FeaturePlace.of(tenant_id, platform, None, agent_id=agent_id)
    resolved = await place_feature_or_none(sessions, settings, place, KEY)
    return resolved is not None and resolved.on
