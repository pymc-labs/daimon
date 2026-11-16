"""Check a Discord channel or thread over REST, as a routine's owner could post there.

For callers with no Discord client (the operator CLI). It reads the same facts the
chat tool reads, from the bot's REST credential, and applies the shared rule
(`discord_creator_may_post`) that delivery applies again when the routine fires.
Nothing is trusted from the caller: the channel must belong to the guild, and a
thread's permissions come from its parent. Inject an httpx transport for tests.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import httpx
from daimon.core.channel_visibility import (
    DISCORD_ADMINISTRATOR,
    DISCORD_MANAGE_THREADS,
    DISCORD_SEND_MESSAGES,
    DISCORD_SEND_MESSAGES_IN_THREADS,
    DISCORD_VIEW_CHANNEL,
    DiscordMember,
    DiscordOverwrite,
    discord_base_permissions,
    discord_channel_permissions,
)
from daimon.core.discord_membership import (
    discord_client,
    discord_get,
)
from daimon.core.routine_delivery import discord_creator_may_post
from daimon.core.routine_service import Placement, RoutineRefused
from daimon.core.stores.domain import RoutineDestinationKind

_TEXT_CHANNELS = (0, 5)
_THREADS = (10, 11, 12)
_PRIVATE_THREAD = 12
_Json = dict[str, object]


def _unverifiable(destination_id: str) -> RoutineRefused:
    return RoutineRefused(
        f"daimon cannot see {destination_id} in this server; check the id and that "
        "daimon has access to it. Nothing was saved."
    )


def _snowflake(value: object) -> str | None:
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return value
    return None


def _overwrites(channel: _Json) -> list[DiscordOverwrite]:
    result: list[DiscordOverwrite] = []
    raw = channel.get("permission_overwrites")
    for item in cast("list[_Json]", raw) if isinstance(raw, list) else []:
        ident, kind = _snowflake(item.get("id")), item.get("type")
        allow, deny = _snowflake(item.get("allow")), _snowflake(item.get("deny"))
        if ident is None or not isinstance(kind, int) or allow is None or deny is None:
            raise ValueError("malformed permission overwrite")
        result.append(DiscordOverwrite(ident, kind, int(allow), int(deny)))
    return result


def _timed_out(member: _Json) -> bool:
    until = member.get("communication_disabled_until")
    if not isinstance(until, str):
        return False
    return datetime.fromisoformat(until) > datetime.now(UTC)


class DiscordRestDestinationResolver:
    """A `DestinationResolver` for one guild and one owner, over the bot's REST credential."""

    def __init__(
        self,
        bot_token: str,
        *,
        guild_id: str,
        owner_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._bot_token = bot_token
        self._guild_id = guild_id
        self._owner_id = owner_id
        self._transport = transport

    async def __call__(self, kind: RoutineDestinationKind, destination_id: str) -> Placement:
        try:
            async with asyncio.timeout(15), discord_client(self._bot_token, self._transport) as c:
                return await self._resolve(c, kind, destination_id)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError) as err:
            raise _unverifiable(destination_id) from err

    async def _get(self, client: httpx.AsyncClient, path: str) -> _Json | None:
        response = await discord_get(client, path)
        if response.status_code in (403, 404):
            return None
        response.raise_for_status()
        raw: object = response.json()
        if not isinstance(raw, dict):
            raise ValueError("unexpected response")
        return cast("_Json", raw)

    async def _resolve(
        self, client: httpx.AsyncClient, kind: RoutineDestinationKind, destination_id: str
    ) -> Placement:
        guild = self._guild_id
        channel = await self._get(client, f"channels/{destination_id}")
        if channel is None or channel.get("id") != destination_id:
            raise _unverifiable(destination_id)
        if channel.get("guild_id") != guild:
            raise RoutineRefused(f"{destination_id} is not in this server. Nothing was saved.")
        channel_type = channel.get("type")
        is_thread = channel_type in _THREADS
        if not is_thread and channel_type not in _TEXT_CHANNELS:
            raise RoutineRefused(f"{destination_id} is not a text channel or thread")
        if is_thread and kind != "thread":
            raise RoutineRefused(f"{destination_id} is a thread: use destination thread")
        if not is_thread and kind != "channel":
            raise RoutineRefused(f"{destination_id} is a channel: use destination channel")
        parent = channel
        if is_thread:
            parent_id = _snowflake(channel.get("parent_id"))
            parent_channel = await self._get(client, f"channels/{parent_id}") if parent_id else None
            if (
                parent_channel is None
                or parent_channel.get("id") != parent_id
                or parent_channel.get("guild_id") != guild
            ):
                raise _unverifiable(destination_id)
            parent = parent_channel
        bits = await self._owner_bits(client, parent)
        administrator = bool(bits & DISCORD_ADMINISTRATOR)
        manage_threads = bool(bits & DISCORD_MANAGE_THREADS)
        is_private = channel_type == _PRIVATE_THREAD
        is_thread_member = False
        if is_private and not (administrator or manage_threads):
            member = await self._get(
                client, f"channels/{destination_id}/thread-members/{self._owner_id}"
            )
            is_thread_member = member is not None
        if not discord_creator_may_post(
            administrator=administrator,
            view_channel=bool(bits & DISCORD_VIEW_CHANNEL),
            send_messages=bool(bits & DISCORD_SEND_MESSAGES),
            is_thread=is_thread,
            send_messages_in_threads=bool(bits & DISCORD_SEND_MESSAGES_IN_THREADS),
            is_private_thread=is_private,
            manage_threads=manage_threads,
            is_thread_member=is_thread_member,
        ):
            raise RoutineRefused(
                f"The owner can't post in {destination_id}, so a routine can't deliver there "
                "for them. Nothing was saved.\n"
                "Pick a channel they can post in, or ask an admin or an owner for access."
            )
        category = _snowflake(parent.get("parent_id"))
        if is_thread:
            return Placement(
                channel_id=destination_id,
                parent_channel_id=cast("str", parent.get("id")),
                category_id=category,
            )
        return Placement(channel_id=destination_id, category_id=category)

    async def _owner_bits(self, client: httpx.AsyncClient, channel: _Json) -> int:
        """The owner's permission bits in `channel`, from their roles and its overwrites."""
        guild, owner = self._guild_id, self._owner_id
        member = await self._get(client, f"guilds/{guild}/members/{owner}")
        guild_data = await self._get(client, f"guilds/{guild}")
        roles = await discord_get(client, f"guilds/{guild}/roles")
        roles.raise_for_status()
        if member is None or guild_data is None or guild_data.get("id") != guild:
            raise _unverifiable(cast("str", channel.get("id")))
        raw_ids = member.get("roles")
        role_ids = (
            [_snowflake(r) for r in cast("list[object]", raw_ids)]
            if isinstance(raw_ids, list)
            else None
        )
        if role_ids is None or None in role_ids:
            raise ValueError("malformed member roles")
        role_bits: dict[str, int] = {}
        for role in cast("list[_Json]", roles.json()):
            role_id, raw = _snowflake(role.get("id")), _snowflake(role.get("permissions"))
            if role_id is None or raw is None:
                raise ValueError("malformed role")
            role_bits[role_id] = int(raw)
        subject = DiscordMember(
            user_id=owner,
            role_ids=frozenset(cast("list[str]", role_ids)),
            is_owner=guild_data.get("owner_id") == owner,
            timed_out=_timed_out(member),
        )
        return discord_channel_permissions(
            subject,
            guild_id=guild,
            base_permissions=discord_base_permissions(
                subject, guild_id=guild, role_permissions=role_bits
            ),
            overwrites=_overwrites(channel),
        )
