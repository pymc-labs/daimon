"""Drain confirmations for bare self-serve GitHub connections."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

import structlog
from daimon.core.stores.github_connect import sweep_expired_click_intents
from daimon.core.stores.github_connect_notices import ConnectNotice, claim_next, expire_old, settle
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


async def poll_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: Literal["discord", "slack"],
    deliver: Callable[[ConnectNotice], Awaitable[bool]],
) -> int:
    delivered = 0
    for _ in range(25):
        async with sessionmaker.begin() as session:
            now = datetime.now(UTC)
            await sweep_expired_click_intents(session, now=now)
            await expire_old(session, platform=platform, now=now)
            notice = await claim_next(session, platform=platform, now=now)
        if notice is None:
            break
        try:
            landed = await deliver(notice)
        except Exception:
            _log.exception("github_connect.delivery_failed", platform=platform)
            landed = False
        async with sessionmaker.begin() as session:
            await settle(session, notice=notice, delivered=landed, now=datetime.now(UTC))
        delivered += int(landed)
    return delivered


async def run_connect_notice_poller(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: Literal["discord", "slack"],
    deliver: Callable[[ConnectNotice], Awaitable[bool]],
    should_stop: Callable[[], bool],
    interval_s: float = 60.0,
) -> None:
    while not should_stop():
        try:
            await poll_once(sessionmaker, platform=platform, deliver=deliver)
        except Exception:
            _log.exception("github_connect.poll_failed", platform=platform)
        await asyncio.sleep(interval_s)
