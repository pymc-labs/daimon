"""Whether a turn's channel may hear from the agent at all.

`admit()` refuses a turn whose reply would land in a write-protected channel,
but adapters post notices of their own before admission (capacity, role
lookup) and render refusals after it. They call `turn_target_protected`
first, at the top of every turn entry, and stay silent -- log only -- when it
says so. When protection can't be established -- the policy doesn't parse,
or the database or pool fails while reading it -- it counts as protected: we
can't tell, so nothing is posted, not even an error.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import structlog
from daimon.core.access_policy import is_write_protected
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Looks up the Discord category of the turn's channel: (category id, unresolved).
CategoryLookup = Callable[[], Awaitable[tuple[str | None, bool]]]

log = structlog.get_logger()


async def turn_target_protected(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    thread_id: str | None = None,
    category_id: str | None = None,
    category_unresolved: bool = False,
    resolve_category: CategoryLookup | None = None,
) -> bool:
    """True when nothing may be posted for a turn in this channel or thread.

    ``resolve_category`` is called only when the policy protects a category,
    so a lookup that costs a platform call is skipped for everyone else.
    """
    try:
        async with sessionmaker() as session:
            policy = await load_access_policy(session, tenant_id=tenant_id)
    except AccessPolicyUnreadable:
        log.warning("access_policy.unreadable", tenant_id=str(tenant_id))
        return True
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        log.warning("access_policy.read_failed", tenant_id=str(tenant_id), error=type(exc).__name__)
        return True
    if resolve_category is not None and policy.protected_category_ids:
        category_id, category_unresolved = await resolve_category()
    return is_write_protected(
        policy,
        channel_id=thread_id or channel_id,
        parent_channel_id=channel_id,
        category_id=category_id,
        category_unresolved=category_unresolved,
    )
