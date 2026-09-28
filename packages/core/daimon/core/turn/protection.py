"""Whether a turn's channel may hear from the agent at all.

`admit()` refuses a turn whose reply would land in a write-protected channel,
but adapters post notices of their own before admission (capacity, role
lookup) and render refusals after it. They call `turn_target_protected`
first, at the top of every turn entry, and stay silent -- log only -- when it
says so. A policy that can't be read counts as protected: we can't tell, so
nothing is posted.
"""

from __future__ import annotations

import uuid

import structlog
from daimon.core.access_policy import is_write_protected
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()


async def turn_target_protected(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    thread_id: str | None = None,
    category_id: str | None = None,
    category_unresolved: bool = False,
) -> bool:
    """True when nothing may be posted for a turn in this channel or thread."""
    try:
        async with sessionmaker() as session:
            policy = await load_access_policy(session, tenant_id=tenant_id)
    except AccessPolicyUnreadable:
        log.warning("access_policy.unreadable", tenant_id=str(tenant_id))
        return True
    return is_write_protected(
        policy,
        channel_id=thread_id or channel_id,
        parent_channel_id=channel_id,
        category_id=category_id,
        category_unresolved=category_unresolved,
    )
