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
from collections.abc import Awaitable, Callable, Collection, Mapping
from typing import Any, Literal

import structlog
from daimon.core.platform_names import ChannelLookup, ChannelStatus
from daimon.core.stores.channel_directory import list_configured_channel_ids
from daimon.core.stores.platform_names import (
    ChannelSighting,
    list_channel_names,
    mark_channel_status,
    upsert_channel_names,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

#: Six hours between rounds: names change rarely and one round lists every workspace.
LISTING_INTERVAL_S = 6 * 3600.0
#: First round a few minutes after start, so a restart storm doesn't list at once.
FIRST_ROUND_S = 300.0
#: Channels with settings that the listing did not show are asked about, this many a round.
STATUS_CHECKS_PER_ROUND = 50
# Discord's "Unknown Channel": the channel was deleted.
_DISCORD_UNKNOWN_CHANNEL = 10003
# Discord channel types for a direct message and a group direct message.
_DISCORD_DIRECT_TYPES = (1, 3)


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


def discord_channel_answer(
    status_code: int, fields: Mapping[str, Any], guild_id: str
) -> str | ChannelStatus | None:
    """What Discord's answer for one channel id says: its name, `GONE`, `DIRECT`, or None.

    `status_code` and `fields` are `GET /channels/{id}`'s status and JSON (an error's
    `code` is in `fields`). None means no answer worth keeping: a refusal, a channel of
    another server (the bot sits in many, and one is not this tenant's to name), or no name.
    """
    if status_code == 404 and fields.get("code") == _DISCORD_UNKNOWN_CHANNEL:
        return ChannelStatus.GONE
    if status_code != 200:
        _log.info("channel_lookup.refused", status=status_code)
        return None
    if fields.get("guild_id") is None and fields.get("type") in _DISCORD_DIRECT_TYPES:
        return ChannelStatus.DIRECT
    if fields.get("guild_id") != guild_id:
        _log.info("channel_lookup.foreign")
        return None
    name = fields.get("name")
    return name if isinstance(name, str) else None


async def check_unlisted_channels(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    external_id: str,
    listed: Collection[str],
    lookup: ChannelLookup,
    limit: int = STATUS_CHECKS_PER_ROUND,
    gap_s: float = 0.0,
) -> int:
    """Ask the platform about channels with settings that the listing did not show.

    A channel it names is stored named again; one it calls deleted or a direct message is
    stored as such. Channels already stored as gone or direct are left alone, and at most
    `limit` are asked per round (a random pick, so none starves); `gap_s` spaces the calls.
    Returns how many answered gone or direct.
    """
    async with sessionmaker() as session:
        configured = await list_configured_channel_ids(
            session, tenant_id=tenant_id, platform=platform
        )
        known = {
            row.channel_id: row
            for row in await list_channel_names(
                session, tenant_id=tenant_id, platform=platform, unnamed=True
            )
        }
    suspects = sorted(
        cid for cid in configured - set(listed) if cid not in known or known[cid].status == "named"
    )
    if len(suspects) > limit:
        suspects = random.sample(suspects, limit)
    names: dict[str, str | ChannelSighting] = {}
    statuses: dict[str, Literal["gone", "direct"]] = {}
    for position, channel_id in enumerate(suspects):
        if position and gap_s:
            await asyncio.sleep(gap_s)
        try:
            answer = await lookup(external_id, channel_id)
        except Exception:  # the platform call is the adapter's; a status is optional
            _log.info("channel_status.lookup_failed", platform=platform)
            continue
        if isinstance(answer, ChannelStatus):
            statuses[channel_id] = "gone" if answer is ChannelStatus.GONE else "direct"
        elif answer:
            names[channel_id] = ChannelSighting(answer)
    if names or statuses:
        async with sessionmaker.begin() as session:
            await upsert_channel_names(session, tenant_id=tenant_id, platform=platform, names=names)
            await mark_channel_status(
                session, tenant_id=tenant_id, platform=platform, statuses=statuses
            )
    return len(statuses)


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


__all__ = [
    "FIRST_ROUND_S",
    "LISTING_INTERVAL_S",
    "STATUS_CHECKS_PER_ROUND",
    "check_unlisted_channels",
    "discord_channel_answer",
    "run_channel_listing",
    "store_listing",
]
