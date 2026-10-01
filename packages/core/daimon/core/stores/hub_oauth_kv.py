"""Expiry deletion for the hub login key-value table.

The proxies write this table through py-key-value-aio; daimon's only access
is pruning rows whose ``expires_at`` has passed. Rows with a NULL
``expires_at`` are permanent (dynamic client registrations) and are never
touched here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from daimon.core._models import HubOAuthKv
from sqlalchemy import CursorResult, delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession


async def delete_expired_hub_oauth_kv(
    session: AsyncSession, *, cutoff: datetime, limit: int
) -> int:
    """Delete up to ``limit`` rows with ``expires_at < cutoff``; return the count."""
    victims = (
        select(HubOAuthKv.collection, HubOAuthKv.key)
        .where(HubOAuthKv.expires_at.is_not(None), HubOAuthKv.expires_at < cutoff)
        .limit(limit)
    )
    result = await session.execute(
        delete(HubOAuthKv).where(tuple_(HubOAuthKv.collection, HubOAuthKv.key).in_(victims))
    )
    return cast(CursorResult[Any], result).rowcount
