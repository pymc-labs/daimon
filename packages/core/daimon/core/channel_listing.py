"""Refresh `platform_channel_names` from each platform's channel list, now and then.

Adapters remember channels as events show them; this fills in the rest (kind,
parent, archived, last seen) from the platform's own list of what the bot can see.
Each adapter supplies `list_once`, run on a slow interval so listing stays far under
the platforms' rate limits; a failed round is logged and retried next round.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Awaitable, Callable, Mapping

import structlog
from daimon.core.stores.platform_names import ChannelSighting, upsert_channel_names
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

#: Six hours between rounds: names change rarely and one round lists every workspace.
LISTING_INTERVAL_S = 6 * 3600.0
#: First round a few minutes after start, so a restart storm doesn't list at once.
FIRST_ROUND_S = 300.0


async def store_listing(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channels: Mapping[str, ChannelSighting],
) -> int:
    """Store one workspace's listed channels; returns how many."""
    if not channels:
        return 0
    async with sessionmaker.begin() as session:
        await upsert_channel_names(
            session, tenant_id=tenant_id, platform=platform, names=dict(channels)
        )
    return len(channels)


async def run_channel_listing(
    list_once: Callable[[], Awaitable[int]],
    *,
    platform: str,
    should_stop: Callable[[], bool],
    interval_s: float = LISTING_INTERVAL_S,
    first_round_s: float = FIRST_ROUND_S,
) -> None:
    """Run `list_once` (it returns how many channels it stored) every `interval_s`."""
    await asyncio.sleep(first_round_s * random.uniform(0.5, 1.5))
    while not should_stop():
        try:
            stored = await list_once()
            _log.info("channel_listing.refreshed", platform=platform, channels=stored)
        except Exception:
            _log.exception("channel_listing.failed", platform=platform)
        await asyncio.sleep(interval_s)


__all__ = ["FIRST_ROUND_S", "LISTING_INTERVAL_S", "run_channel_listing", "store_listing"]
