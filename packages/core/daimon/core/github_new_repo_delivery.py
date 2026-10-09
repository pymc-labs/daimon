"""Drain new GitHub repo notices into private platform cards."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

import structlog
from daimon.core.stores.github_new_repo_notices import (
    NewRepoNoticeGroup,
    claim_notice_group,
    finish_notice,
    pending_notice_tenants,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DeliverGroup = Callable[[NewRepoNoticeGroup], Awaitable[bool]]
_log = structlog.get_logger(__name__)


async def poll_new_repo_notices_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: Literal["discord", "slack"],
    deliver: DeliverGroup,
) -> int:
    async with sessionmaker() as session:
        tenants = await pending_notice_tenants(session, platform=platform)
    sent = 0
    for tenant_id in tenants:
        now = datetime.now(UTC)
        async with sessionmaker.begin() as session:
            group = await claim_notice_group(session, tenant_id=tenant_id, now=now)
        if group is None:
            continue
        try:
            landed = await deliver(group)
        except Exception:
            _log.exception("github_new_repo.delivery_failed", platform=platform)
            landed = False
        async with sessionmaker.begin() as session:
            for notice in group.notices:
                await finish_notice(session, notice=notice, delivered=landed, now=datetime.now(UTC))
        sent += int(landed)
    return sent
