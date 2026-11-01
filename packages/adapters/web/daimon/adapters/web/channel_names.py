"""Channel names the web app shows, for channels the store has not seen yet."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any

import structlog
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.platform_names import ChannelLookup, lookup_channel_names
from daimon.core.stores import platform_names
from daimon.core.stores.tenants import get_tenant

log = structlog.get_logger(__name__)

_DISCORD_API = "https://discord.com/api/v10"


def channel_lookup_of(runtime: WebRuntime) -> ChannelLookup | None:
    """Ask Discord for a channel's name where this process holds the bot token.

    Slack and Teams name their channels from events their own adapters see; this process
    holds no workspace token for them.
    """
    discord = runtime.settings.discord
    if discord is None:
        return None
    headers = {"Authorization": f"Bot {discord.bot_token.get_secret_value()}"}

    async def lookup(channel_id: str) -> str | None:
        if not channel_id.isdigit():
            return None
        response = await runtime.http_client.get(
            f"{_DISCORD_API}/channels/{channel_id}", headers=headers
        )
        if response.status_code != 200:
            log.info("web.channel_lookup_refused", status=response.status_code)
            return None
        body: dict[str, Any] = response.json() if isinstance(response.json(), dict) else {}
        name = body.get("name")
        return name if isinstance(name, str) else None

    return lookup


async def channel_names(
    runtime: WebRuntime, tenant_id: uuid.UUID, channel_ids: Collection[str]
) -> tuple[str, dict[str, str]]:
    """The tenant's platform and each of `channel_ids` that has a name: stored, else asked once."""
    async with runtime.sessionmaker() as db:
        tenant = await get_tenant(db, tenant_id)
        platform = tenant.platform if tenant is not None else "discord"
        names = await platform_names.get_channel_names(
            db, tenant_id=tenant_id, platform=platform, channel_ids=channel_ids
        )
    lookup = channel_lookup_of(runtime)
    if lookup is not None and (missing := set(channel_ids) - names.keys()):
        names |= await lookup_channel_names(
            runtime.sessionmaker,
            tenant_id=tenant_id,
            platform=platform,
            channel_ids=missing,
            lookup=lookup,
        )
    return platform, names


async def named_spend_channels(
    runtime: WebRuntime,
    tenant_id: uuid.UUID,
    names: dict[tuple[str, str], str],
    channels: Collection[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """`names` plus Discord channels among `channels` that the store had no name for."""
    lookup = channel_lookup_of(runtime)
    missing = {cid for platform, cid in channels if platform == "discord" and cid} - {
        cid for platform, cid in names if platform == "discord"
    }
    if lookup is None or not missing:
        return names
    found = await lookup_channel_names(
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="discord",
        channel_ids=missing,
        lookup=lookup,
    )
    return names | {("discord", cid): name for cid, name in found.items()}
