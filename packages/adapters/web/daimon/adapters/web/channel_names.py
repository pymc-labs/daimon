"""Channel names the web app shows, for channels the store has not seen yet."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any, cast

import httpx
import structlog
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.platform_names import (
    ChannelLookup,
    ChannelStatus,
    LookedUpChannels,
    lookup_channel_names,
)
from daimon.core.stores import platform_names
from daimon.core.stores.tenants import get_tenant

log = structlog.get_logger(__name__)

_DISCORD_API = "https://discord.com/api/v10"
# Discord's "Unknown Channel": the channel was deleted.
_UNKNOWN_CHANNEL = 10003
# Discord channel types for a direct message and a group direct message.
_DIRECT_TYPES = (1, 3)


def channel_lookup_of(runtime: WebRuntime) -> ChannelLookup | None:
    """Ask Discord for a channel's name where this process holds the bot token.

    Slack and Teams name their channels from events their own adapters see; this process
    holds no workspace token for them.
    """
    discord = runtime.settings.discord
    if discord is None:
        return None
    headers = {"Authorization": f"Bot {discord.bot_token.get_secret_value()}"}

    async def lookup(guild_id: str, channel_id: str) -> str | ChannelStatus | None:
        if not channel_id.isdigit():
            return None
        try:
            response = await runtime.http_client.get(
                f"{_DISCORD_API}/channels/{channel_id}", headers=headers
            )
        except httpx.HTTPError as exc:
            log.info("web.channel_lookup_failed", error=type(exc).__name__)
            return None
        fields = _json_fields(response)
        if response.status_code == 404 and fields.get("code") == _UNKNOWN_CHANNEL:
            return ChannelStatus.GONE
        if response.status_code != 200:
            log.info("web.channel_lookup_refused", status=response.status_code)
            return None
        if fields.get("guild_id") is None and fields.get("type") in _DIRECT_TYPES:
            return ChannelStatus.DIRECT
        # The bot sits in many servers; a channel of another one is not this tenant's to name.
        if fields.get("guild_id") != guild_id:
            log.info("web.channel_lookup_foreign")
            return None
        name = fields.get("name")
        return name if isinstance(name, str) else None

    return lookup


def _json_fields(response: httpx.Response) -> dict[str, Any]:
    try:
        body: Any = response.json()
    except ValueError:
        return {}
    return cast("dict[str, Any]", body) if isinstance(body, dict) else {}


async def channel_names(
    runtime: WebRuntime, tenant_id: uuid.UUID, channel_ids: Collection[str]
) -> tuple[str, LookedUpChannels]:
    """The tenant's platform and what each of `channel_ids` is called: stored, else asked once.

    The platform's answer also says which channels are gone or direct messages.
    """
    async with runtime.sessionmaker() as db:
        tenant = await get_tenant(db, tenant_id)
        platform = tenant.platform if tenant is not None else "discord"
        external_id = tenant.external_id if tenant is not None else ""
        names = await platform_names.get_channel_names(
            db, tenant_id=tenant_id, platform=platform, channel_ids=channel_ids
        )
    lookup = channel_lookup_of(runtime)
    if (
        lookup is not None
        and platform == "discord"
        and external_id
        and (missing := set(channel_ids) - names.keys())
    ):
        found = await lookup_channel_names(
            runtime.sessionmaker,
            tenant_id=tenant_id,
            platform=platform,
            external_id=external_id,
            channel_ids=missing,
            lookup=lookup,
        )
        return platform, LookedUpChannels(names=names | found.names, statuses=found.statuses)
    return platform, LookedUpChannels(names=names)


async def named_spend_channels(
    runtime: WebRuntime,
    tenant_id: uuid.UUID,
    names: dict[tuple[str, str], str],
    channels: Collection[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """`names` plus labels for Discord channels among `channels` that the store had no name for.

    A deleted channel or a direct message gets the label its status says.
    """
    lookup = channel_lookup_of(runtime)
    missing = {cid for platform, cid in channels if platform == "discord" and cid} - {
        cid for platform, cid in names if platform == "discord"
    }
    if lookup is None or not missing:
        return names
    async with runtime.sessionmaker() as db:
        tenant = await get_tenant(db, tenant_id)
    if tenant is None or tenant.platform != "discord":
        return names
    found = await lookup_channel_names(
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="discord",
        external_id=tenant.external_id,
        channel_ids=missing,
        lookup=lookup,
    )
    return names | {("discord", cid): name for cid, name in found.labels().items()}
