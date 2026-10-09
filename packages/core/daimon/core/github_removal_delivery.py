"""Drain GitHub-side removal notices into private admin cards."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

import structlog
from daimon.core.stores.github_removal_notices import (
    RemovalNotice,
    claim_notice,
    finish_notice,
    pending_tenants,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Deliver = Callable[[RemovalNotice], Awaitable[bool]]
_log = structlog.get_logger(__name__)


async def poll_removal_notices_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: Literal["discord", "slack"],
    deliver: Deliver,
) -> int:
    async with sessionmaker() as session:
        tenants = await pending_tenants(session, platform=platform)
    sent = 0
    for tenant_id in tenants:
        async with sessionmaker.begin() as session:
            notice = await claim_notice(session, tenant_id=tenant_id, now=datetime.now(UTC))
        if notice is None:
            continue
        try:
            landed = await deliver(notice)
        except Exception:
            _log.exception("github_removal.delivery_failed", platform=platform)
            landed = False
        async with sessionmaker.begin() as session:
            await finish_notice(session, notice=notice, delivered=landed, now=datetime.now(UTC))
        sent += int(landed)
    return sent
