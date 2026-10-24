"""Discord REST membership transport for gateway, scheduler and MCP hosts.

Uses the existing bot credential. No gateway/adapter imports. Both the member
and guild role permissions must resolve: stored account roles are never admin
proof. The resolver owns caching; transport retries rate limits only within
its ten-second lookup deadline. Inject an httpx transport for offline tests.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import cast
from uuid import UUID

import httpx
from daimon.core.membership import CachedMembershipResolver, Membership


async def _discord_get(client: httpx.AsyncClient, path: str) -> httpx.Response:
    for _ in range(3):
        response = await client.get(path)
        if response.status_code != 429:
            return response
        retry: object = response.json().get("retry_after")
        if not isinstance(retry, int | float) or not 0 <= retry <= 3:
            raise ValueError("unbounded server rate limit")
        await asyncio.sleep(retry)
    raise ValueError("server rate limit exhausted")


def _discord_client(
    bot_token: str, transport: httpx.AsyncBaseTransport | None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://discord.com/api/v10/",
        headers={"Authorization": f"Bot {bot_token}"},
        timeout=5,
        transport=transport,
    )


class DiscordRestMembershipResolver(CachedMembershipResolver):
    def __init__(
        self,
        bot_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._bot_token = bot_token
        self._transport = transport
        super().__init__(self._fetch_member, clock=clock)

    async def _fetch_member(self, tenant_id: UUID, guild_id: str, user_id: str) -> Membership:
        del tenant_id  # Tenant binding is enforced by the caller and cache key.
        if not guild_id.isdecimal() or not user_id.isdecimal():
            return Membership()
        async with _discord_client(self._bot_token, self._transport) as client:
            member = await _discord_get(client, f"guilds/{guild_id}/members/{user_id}")
            if member.status_code == 404:
                return Membership(is_member=False)
            member.raise_for_status()
            data = cast("dict[str, object]", member.json())
            user = data.get("user")
            if not isinstance(user, dict) or cast("dict[str, object]", user).get("id") != user_id:
                return Membership()
            raw_roles = data.get("roles")
            if not isinstance(raw_roles, list):
                return Membership()
            values = cast("list[object]", raw_roles)
            if not all(isinstance(r, str) and r.isascii() and r.isdecimal() for r in values):
                return Membership()
            if len(set(cast("list[str]", raw_roles))) != len(values):
                return Membership()
            roles = tuple(r for r in cast("list[str]", raw_roles) if r != guild_id)
            guild = await _discord_get(client, f"guilds/{guild_id}")
            guild.raise_for_status()
            role_response = await _discord_get(client, f"guilds/{guild_id}/roles")
            role_response.raise_for_status()
            guild_data = cast("dict[str, object]", guild.json())
            if guild_data.get("id") != guild_id or not isinstance(guild_data.get("owner_id"), str):
                return Membership()
            permissions = 0
            by_id: dict[str, int] = {}
            for role in cast("list[dict[str, object]]", role_response.json()):
                role_id = role["id"]
                raw: object = role["permissions"]
                if (
                    not isinstance(role_id, str)
                    or not role_id.isascii()
                    or not role_id.isdecimal()
                    or not isinstance(raw, str)
                    or not raw.isascii()
                    or not raw.isdecimal()
                    or role_id in by_id
                    or int(raw) >= 2**64
                ):
                    return Membership()
                by_id[role_id] = int(raw)
            if any(role not in by_id for role in (*roles, guild_id)):
                return Membership()
            for role_id in (*roles, guild_id):
                permissions |= by_id[role_id]
            return Membership(
                is_member=True,
                role_ids=roles,
                is_admin=guild_data.get("owner_id") == user_id or bool(permissions & (8 | 32)),
            )


async def verify_discord_source(
    bot_token: str,
    *,
    guild_id: str,
    channel_id: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, str | None]:
    """Resolve a routine source to (budget parent, original thread), failing closed.

    Both a thread and its parent must belong to the selected guild. Only text
    and announcement channels, or threads under text/announcement/forum/media
    channels, supply routine context. No caller-supplied parent is trusted.
    """
    from daimon.core.errors import StoreError

    if not channel_id.isascii() or not channel_id.isdecimal() or not guild_id.isdecimal():
        raise StoreError("the source must be a channel or thread ID")
    try:
        async with (
            asyncio.timeout(10),
            _discord_client(bot_token, transport) as client,
        ):
            response = await _discord_get(client, f"channels/{channel_id}")
            response.raise_for_status()
            raw: object = response.json()
            if not isinstance(raw, dict):
                raise StoreError("the source channel could not be verified; nothing was saved")
            channel = cast("dict[str, object]", raw)
            if channel.get("id") != channel_id or channel.get("guild_id") != guild_id:
                raise StoreError("the source is not in the selected tenant's guild")
            if channel.get("type") in (0, 5):
                return channel_id, None
            if channel.get("type") not in (10, 11, 12):
                raise StoreError("the source must be a text channel or thread")
            parent_id = channel.get("parent_id")
            if (
                not isinstance(parent_id, str)
                or not parent_id.isascii()
                or not parent_id.isdecimal()
            ):
                raise StoreError("the source thread has no valid parent")
            response = await _discord_get(client, f"channels/{parent_id}")
            response.raise_for_status()
            raw_parent: object = response.json()
            if not isinstance(raw_parent, dict):
                raise StoreError("the source parent could not be verified; nothing was saved")
            parent = cast("dict[str, object]", raw_parent)
            if (
                parent.get("id") != parent_id
                or parent.get("guild_id") != guild_id
                or parent.get("type") not in (0, 5, 15, 16)
            ):
                raise StoreError(
                    "the source thread's parent is not a supported channel in this guild"
                )
            return parent_id, channel_id
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        raise StoreError("the source channel could not be verified; nothing was saved") from exc
