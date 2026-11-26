"""A fictional Discord server, as discord.py-shaped fakes and as Discord's REST API returns
it, for the room check."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
from daimon.adapters.discord.audience_rest import DiscordRestReader
from pydantic import SecretStr

GUILD = 4000
CORE, FRIEND = 4101, 4102
VIEW = discord.Permissions(view_channel=True)


def role(role_id: int, *, view: bool = False) -> Any:
    fake = MagicMock(spec=discord.Role)
    fake.id = role_id
    fake.permissions = discord.Permissions(view_channel=view, read_message_history=view)
    fake.is_default.return_value = role_id == GUILD
    return fake


def member(user_id: int, roles: list[Any], *, bot: bool = False, timed_out: bool = False) -> Any:
    fake = MagicMock(spec=discord.Member)
    fake.id, fake.bot, fake.roles = user_id, bot, [role(GUILD), *roles]
    fake.is_timed_out.return_value = timed_out
    fake.guild_permissions = discord.Permissions()
    return fake


def target(kind: type, target_id: int) -> discord.Object:
    return discord.Object(id=target_id, type=kind)  # type: ignore[arg-type]


def channel(
    channel_id: int,
    name: str,
    overwrites: dict[discord.Object, discord.PermissionOverwrite],
    *,
    kind: discord.ChannelType = discord.ChannelType.text,
) -> Any:
    fake = MagicMock(spec=discord.TextChannel)
    fake.id, fake.name, fake.type, fake.overwrites = channel_id, name, kind, overwrites
    return fake


def thread(thread_id: int, parent_id: int, *, private: bool = False, name: str = "thread") -> Any:
    fake = MagicMock(spec=discord.Thread)
    fake.id, fake.parent_id, fake.name = thread_id, parent_id, name
    fake.type = discord.ChannelType.private_thread if private else discord.ChannelType.public_thread
    return fake


def internal_only() -> dict[discord.Object, discord.PermissionOverwrite]:
    return {
        target(discord.Role, GUILD): discord.PermissionOverwrite(view_channel=False),
        target(discord.Role, CORE): discord.PermissionOverwrite(view_channel=True),
        target(discord.Role, FRIEND): discord.PermissionOverwrite(view_channel=True),
    }


def guild(*, channels: list[Any], threads: list[Any] | None = None, chunked: bool = True) -> Any:
    """Five core members, one friend, one outsider and a bot. Roles give core and friend a
    base view; @everyone has none."""
    fake = MagicMock(spec=discord.Guild)
    core, friend = role(CORE, view=True), role(FRIEND, view=True)
    fake.id, fake.owner_id, fake.chunked, fake.name = GUILD, 1, chunked, "Fictional"
    fake.roles = [role(GUILD), core, friend]
    fake.members = [
        *(member(10 + n, [core]) for n in range(5)),
        member(20, [friend]),
        member(30, []),
        member(40, [], bot=True),
    ]
    fake.channels = channels
    fake.threads = threads or []
    # REST reads a reconcile makes; a test changes what they return to model a missed event.
    fake.fetch_roles = AsyncMock(side_effect=lambda: list(fake.roles))
    fake.fetch_channels = AsyncMock(side_effect=lambda: list(fake.channels))

    async def chunk(**_kw: Any) -> list[Any]:
        return list(fake.members)  # `chunked` is discord.py's count check: it stays as given

    fake.chunk = AsyncMock(side_effect=chunk)
    for each in (*channels, *fake.threads):
        each.guild = fake
    return fake


# --- the same server as Discord's REST API returns it ----------------------------------

VIEW_BIT, READ_BIT = 1 << 10, 1 << 16
OWNER = 10
OUTSIDER = 30
API = "https://discord.test/api/v10"


def rest_overwrite(
    target_id: int, *, member: bool = False, allow: int = 0, deny: int = 0
) -> dict[str, Any]:
    return {"id": str(target_id), "type": int(member), "allow": str(allow), "deny": str(deny)}


def rest_internal_only() -> list[dict[str, Any]]:
    return [
        rest_overwrite(GUILD, deny=VIEW_BIT),
        rest_overwrite(CORE, allow=VIEW_BIT),
        rest_overwrite(FRIEND, allow=VIEW_BIT),
    ]


def rest_member(user_id: int, *roles: int, bot: bool = False) -> dict[str, Any]:
    return {
        "user": {"id": str(user_id), "bot": bot},
        "roles": [str(r) for r in roles],
        "communication_disabled_until": None,
    }


Hook = Callable[[httpx.Request], Awaitable[httpx.Response | None]]


class RestServer:
    """One fictional server: five core members (the first owns it), a friend, an outsider
    and a bot. Core and friend roles give a base view; @everyone has none. Room 1 is
    internal only, room 2 also lets the outsider in, thread 3 is private in room 1."""

    def __init__(self, guild_id: int = GUILD) -> None:
        self.guild_id = guild_id
        self.owner_id = OWNER
        self.roles: dict[int, int] = {
            guild_id: 0,
            CORE: VIEW_BIT | READ_BIT,
            FRIEND: VIEW_BIT | READ_BIT,
        }
        guest = [*rest_internal_only(), rest_overwrite(OUTSIDER, member=True, allow=VIEW_BIT)]
        self.channels: list[dict[str, Any]] = [
            {
                "id": "1",
                "type": 0,
                "name": "planning",
                "permission_overwrites": rest_internal_only(),
            },
            {"id": "2", "type": 0, "name": "guest", "permission_overwrites": guest},
        ]
        self.threads: list[dict[str, Any]] = [
            {"id": "3", "type": 12, "name": "thread", "parent_id": "1"}
        ]
        self.members: list[dict[str, Any]] = [
            *(rest_member(10 + n, CORE) for n in range(5)),
            rest_member(20, FRIEND),
            rest_member(OUTSIDER),
            rest_member(40, bot=True),
        ]
        #: Called before each answer by route ("guild", "channels", "threads", "members"):
        #: it may wait, or answer instead (a 429, an error).
        self.hooks: dict[str, Hook] = {}
        self.reads: dict[str, int] = {"guild": 0, "channels": 0, "threads": 0, "members": 0}

    def payload(self, route: str, params: Mapping[str, str]) -> object:
        if route == "guild":
            return {
                "id": str(self.guild_id),
                "owner_id": str(self.owner_id),
                "roles": [{"id": str(i), "permissions": str(p)} for i, p in self.roles.items()],
            }
        if route == "channels":
            return self.channels
        if route == "threads":
            return {"threads": self.threads, "members": []}
        after, limit = int(params.get("after", "0")), int(params.get("limit", "1"))
        ordered = sorted(self.members, key=lambda m: int(m["user"]["id"]))
        return [m for m in ordered if int(m["user"]["id"]) > after][:limit]


class RestDiscord:
    """Discord's REST API for a few fictional servers, through `httpx.MockTransport`."""

    def __init__(self, *servers: RestServer) -> None:
        self.servers = {str(s.guild_id): s for s in servers}
        self.log: list[str] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.removeprefix("/api/v10/").split("/")
        server = self.servers[parts[1]]
        route = "guild" if len(parts) == 2 else ("threads" if parts[2] == "threads" else parts[2])
        self.log.append(f"{parts[1]}:{route}")
        server.reads[route] += 1
        if (hook := server.hooks.get(route)) is not None and (answer := await hook(request)):
            return answer
        return httpx.Response(200, json=server.payload(route, request.url.params))

    def reader(self, **kwargs: Any) -> DiscordRestReader:
        options: dict[str, Any] = {"retry_backoff_s": 0.001, **kwargs}
        return DiscordRestReader(
            SecretStr("fictional-token"),
            api=API,
            transport=httpx.MockTransport(self.handler),
            **options,
        )


def gateway_guild(guild_id: int = GUILD) -> Any:
    """The discord.py guild the bot hands the publisher: only its id and availability."""
    fake = MagicMock(spec=discord.Guild)
    fake.id, fake.unavailable = guild_id, False
    return fake
