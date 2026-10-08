"""Post one final line when GitHub access did not arrive in seven days."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

import structlog
from daimon.core.stores.github_access_requests import (
    AccessRequest,
    claim_due_expiry_group,
    mark_expiry_notice_sent,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PostExpiry = Callable[[AccessRequest], Awaitable[bool]]
_log = structlog.get_logger(__name__)


async def poll_expired_requests_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: Literal["discord", "slack"],
    post: PostExpiry,
    now: datetime | None = None,
    limit: int = 20,
) -> int:
    """Claim, post and settle groups while the row locks prevent duplicate posts."""
    current = now or datetime.now(UTC)
    delivered = 0
    for _ in range(limit):
        async with sessionmaker.begin() as session:
            group = await claim_due_expiry_group(session, platform=platform, now=current)
            if not group:
                break
            if not await post(group[0]):
                break
            await mark_expiry_notice_sent(
                session, request_ids=tuple(row.id for row in group), now=current
            )
            delivered += 1
    return delivered


async def run_request_expiry_poller(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: Literal["discord", "slack"],
    post: PostExpiry,
    should_stop: Callable[[], bool],
    interval_s: float = 60.0,
) -> None:
    while not should_stop():
        try:
            await poll_expired_requests_once(sessionmaker, platform=platform, post=post)
        except Exception:
            _log.exception("github_request.expiry_poll_failed", platform=platform)
        await asyncio.sleep(interval_s)
