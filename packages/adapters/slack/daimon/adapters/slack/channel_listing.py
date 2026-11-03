"""List each workspace's channels the bot can see into `platform_channel_names`.

Slack's `conversations.list` is a tier 2 method (about 20 calls a minute), so pages
are spaced out; a round runs every few hours (`daimon.core.channel_listing`).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any, cast

from daimon.core.channel_listing import check_unlisted_channels, store_listing
from daimon.core.platform_names import ChannelLookup, ChannelStatus
from daimon.core.stores.platform_names import ChannelSighting
from daimon.core.stores.tenants import list_tenants_by_platform
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: Seconds between pages: well under tier 2's limit.
PAGE_GAP_S = 3.5
#: Seconds between `conversations.info` calls (tier 3, about 50 a minute).
INFO_GAP_S = 1.5
#: A Slack channel, group or direct message id: `C`, `G` or `D` and capitals or digits.
_SLACK_ID = re.compile(r"[CGD][A-Z0-9]+")


async def list_workspace(
    client: AsyncWebClient, *, page_gap_s: float = PAGE_GAP_S
) -> dict[str, ChannelSighting]:
    """Every public channel, and private one the bot is in, with whether it is archived."""
    found: dict[str, ChannelSighting] = {}
    cursor: str | None = None
    while True:
        resp = await client.conversations_list(  # pyright: ignore[reportUnknownMemberType]
            types="public_channel,private_channel",
            exclude_archived=False,
            limit=200,
            cursor=cursor,
        )
        data = cast(dict[str, Any], resp.data)
        for ch in cast(list[dict[str, Any]], data.get("channels") or []):
            if ch.get("is_private") and not ch.get("is_member"):
                continue
            found[str(ch["id"])] = ChannelSighting(
                str(ch.get("name") or ch["id"]),
                kind="private" if ch.get("is_private") else "public",
                archived=bool(ch.get("is_archived")),
            )
        meta = cast(dict[str, Any], data.get("response_metadata") or {})
        cursor = cast(str | None, meta.get("next_cursor")) or None
        if cursor is None:
            return found
        await asyncio.sleep(page_gap_s)


def info_lookup(client: AsyncWebClient) -> ChannelLookup:
    """Ask Slack what one channel id is: its name or `DIRECT`.

    Never `GONE`: a private channel the bot is not in answers `channel_not_found` too, so
    that error is no answer. Deletion is marked from `channel_deleted` events instead
    (`setup_conversations.handle_setup_lifecycle`). Ids that are not Slack's (web chat, a
    routine's destination) are not asked about.
    """

    async def lookup(_workspace_id: str, channel_id: str) -> str | ChannelStatus | None:
        if not _SLACK_ID.fullmatch(channel_id):
            return None
        try:
            resp = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]
        except SlackApiError:
            return None
        data = cast("dict[str, Any]", resp.data)  # pyright: ignore[reportUnknownMemberType]
        channel = cast("dict[str, Any]", data.get("channel") or {})
        if channel.get("is_im") or channel.get("is_mpim"):
            return ChannelStatus.DIRECT
        name = channel.get("name")
        return name if isinstance(name, str) else None

    return lookup


async def list_all_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    client_for: Callable[[str], Awaitable[AsyncWebClient | None]],
    *,
    page_gap_s: float = PAGE_GAP_S,
    info_gap_s: float = INFO_GAP_S,
) -> int:
    """One round: every Slack workspace with a bot token, one after another.

    Channels with settings that the list did not show are asked about too (`info_lookup`).
    """
    stored = 0
    for tenant in await list_tenants_by_platform(sessionmaker, platform="slack"):
        client = await client_for(tenant.external_id)
        if client is None:
            continue
        listed = await list_workspace(client, page_gap_s=page_gap_s)
        stored += await store_listing(
            sessionmaker, tenant_id=tenant.id, platform="slack", channels=listed
        )
        await check_unlisted_channels(
            sessionmaker,
            tenant_id=tenant.id,
            platform="slack",
            external_id=tenant.external_id,
            listed=listed.keys(),
            lookup=info_lookup(client),
            gap_s=info_gap_s,
        )
    return stored
