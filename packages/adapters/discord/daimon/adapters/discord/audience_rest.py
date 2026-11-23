"""Reads one Discord server by REST for its room facts: our own reader, not discord.py's.

`DiscordRestReader.read` fetches the server (its owner and roles), its channels, its
active threads and every member page, and turns the raw payloads straight into the
inputs of `daimon.core.audience_facts.compute_room_facts`. A member's roles are the raw
role ids, checked against the role list read with them. Nothing comes from the gateway
cache.

A read is all or nothing. It raises `ReadFailed` for a missing or malformed field, a
member holding a role the role list lacks, an owner who is not a member, a member page
out of order, or a scan that did not reach the last page. A failed read publishes
nothing.

The whole read has one deadline (`SCAN_DEADLINE_S`), and each request is retried a few
times. A 429 sets a cooldown, global or for its route, that every later request of this
reader waits out, in this read and the next; a wait that would pass the deadline fails
the read at once. All of this is plain asyncio work over httpx, so cancelling a read
leaves no lock or flag behind (discord.py's own HTTP client can, so it is never used
here or cancelled).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

import httpx
import structlog
from daimon.core.audience_facts import AudienceChannel, AudienceMember, compute_room_facts
from daimon.core.channel_visibility import DiscordOverwrite
from daimon.core.config import Settings
from daimon.core.stores.audience_facts import StoredRoom
from pydantic import SecretStr

log = structlog.get_logger(__name__)

DISCORD_API = "https://discord.com/api/v10"
#: Discord's largest member page.
MEMBER_PAGE = 1000
#: A whole read (every request, retry and cooldown) must end within this.
SCAN_DEADLINE_S = 60.0
#: One request must answer within this.
REQUEST_TIMEOUT_S = 10.0
#: Tries per request, for a 429, a 5xx or a lost connection.
ATTEMPTS = 4
#: The wait before a retry that Discord gave no time for.
RETRY_BACKOFF_S = 0.5
#: Thread channel types: none is in the channel list, all come from the active list.
_THREAD_TYPES = frozenset({10, 11, 12})


class ReadFailed(Exception):
    """The server could not be read in full. `reason` is safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class GuildSnapshot:
    """One full read of a server, as the room rule needs it."""

    guild_id: str
    owner_id: str
    role_permissions: Mapping[str, int]
    """Every role's permission bits, @everyone's (the server id) included."""
    channels: tuple[AudienceChannel, ...]
    """The server's channels, then its active threads."""
    members: tuple[AudienceMember, ...]
    member_pages: int


@dataclass
class RateLimits:
    """Cooldowns from Discord's 429s, on the loop's monotonic clock.

    Shared by every read of one reader and kept between reads: a global 429 holds back
    every route, a route 429 only its own. Plain numbers, so a cancelled wait leaves
    them as they were."""

    global_until: float = 0.0
    routes: dict[str, float] = field(default_factory=dict[str, float])

    def until(self, route: str) -> float:
        return max(self.global_until, self.routes.get(route, 0.0))


class DiscordRestReader:
    """Reads servers with the bot token. One per process; reads may run at once."""

    def __init__(
        self,
        token: SecretStr,
        *,
        api: str = DISCORD_API,
        transport: httpx.AsyncBaseTransport | None = None,
        scan_deadline_s: float = SCAN_DEADLINE_S,
        request_timeout_s: float = REQUEST_TIMEOUT_S,
        attempts: int = ATTEMPTS,
        retry_backoff_s: float = RETRY_BACKOFF_S,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._token = token
        self._api = api
        self._transport = transport
        self._scan_deadline_s = scan_deadline_s
        self._request_timeout_s = request_timeout_s
        self._attempts = attempts
        self._retry_backoff_s = retry_backoff_s
        self._monotonic = monotonic
        self.limits = RateLimits()

    def _now(self) -> float:
        return self._monotonic() if self._monotonic else asyncio.get_running_loop().time()

    async def read(self, guild_id: int, *, now: datetime) -> GuildSnapshot:
        """The server in full, or `ReadFailed`. `now` decides who is timed out."""
        deadline = self._now() + self._scan_deadline_s
        headers = {
            "Authorization": f"Bot {self._token.get_secret_value()}",
            "User-Agent": "DiscordBot (https://github.com/pymc-labs/daimon, 1)",
        }
        gid = str(guild_id)
        try:
            async with (
                asyncio.timeout(self._scan_deadline_s),
                httpx.AsyncClient(
                    base_url=self._api,
                    headers=headers,
                    transport=self._transport,
                    timeout=self._request_timeout_s,
                ) as http,
            ):

                async def get(
                    route: str, path: str, params: dict[str, str] | None = None
                ) -> object:
                    return await self._get(http, route, path, params, deadline=deadline)

                async with asyncio.TaskGroup() as group:
                    guild = group.create_task(get(f"guild:{gid}", f"/guilds/{gid}"))
                    channels = group.create_task(get(f"channels:{gid}", f"/guilds/{gid}/channels"))
                    threads = group.create_task(
                        get(f"threads:{gid}", f"/guilds/{gid}/threads/active")
                    )
                    pages = group.create_task(self._member_pages(gid, get))
        except TimeoutError:
            raise ReadFailed("deadline") from None
        except ExceptionGroup as group_error:
            first = _first(group_error)
            if isinstance(first, ReadFailed):
                raise first from None
            if isinstance(first, httpx.HTTPError):
                raise ReadFailed(type(first).__name__) from None
            raise
        return snapshot_from_payloads(
            gid,
            guild=guild.result(),
            channels=channels.result(),
            threads=threads.result(),
            member_pages=pages.result(),
            now=now,
        )

    async def _member_pages(self, gid: str, get: Callable[..., Awaitable[object]]) -> list[object]:
        """Every member page, to the first short one. A full last page needs one more
        request, which comes back empty."""
        pages: list[object] = []
        after = "0"
        while True:
            page = await get(
                f"members:{gid}",
                f"/guilds/{gid}/members",
                {"limit": str(MEMBER_PAGE), "after": after},
            )
            members = _list(page, "members")
            pages.append(members)
            if len(members) < MEMBER_PAGE:
                return pages
            after = _member_id(members[-1])

    async def _get(
        self,
        http: httpx.AsyncClient,
        route: str,
        path: str,
        params: dict[str, str] | None,
        *,
        deadline: float,
    ) -> object:
        for _attempt in range(self._attempts):
            wait = self.limits.until(route) - self._now()
            if wait > 0:
                if self._now() + wait > deadline:
                    raise ReadFailed("rate_limited")
                await asyncio.sleep(wait)
            try:
                response = await http.get(path, params=params)
            except httpx.TransportError:
                await self._backoff(deadline)
                continue
            self._note_bucket(route, response)
            if response.status_code == 429:
                self._note_429(route, response)
                continue
            if response.status_code >= 500:
                await self._backoff(deadline)
                continue
            if response.status_code != 200:
                raise ReadFailed(f"http_{response.status_code}")
            try:
                return response.json()
            except ValueError:
                raise ReadFailed("not_json") from None
        raise ReadFailed("attempts_exhausted")

    async def _backoff(self, deadline: float) -> None:
        if self._now() + self._retry_backoff_s > deadline:
            raise ReadFailed("deadline")
        await asyncio.sleep(self._retry_backoff_s)

    def _note_bucket(self, route: str, response: httpx.Response) -> None:
        """A bucket with no requests left cools the route until it resets."""
        if response.headers.get("X-RateLimit-Remaining") == "0":
            reset = _seconds(response.headers.get("X-RateLimit-Reset-After"))
            if reset is not None:
                self._cool(route, reset, is_global=False)

    def _note_429(self, route: str, response: httpx.Response) -> None:
        body: dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                body = cast("dict[str, Any]", parsed)
        except ValueError:
            pass  # Cloudflare's own 429 page: only the header says how long
        retry_after = _seconds(body.get("retry_after"))
        if retry_after is None:
            retry_after = _seconds(response.headers.get("Retry-After"))
        is_global = body.get("global") is True or response.headers.get("X-RateLimit-Global") in (
            "true",
            "True",
        )
        if body == {} and response.headers.get("X-RateLimit-Scope") is None:
            is_global = True  # not Discord's API answering: hold every route back
        self._cool(route, self._retry_backoff_s if retry_after is None else retry_after, is_global)
        log.warning(
            "discord.audience.rate_limited",
            route=route.split(":", 1)[0],
            is_global=is_global,
            retry_after=retry_after,
        )

    def _cool(self, route: str, seconds: float, is_global: bool) -> None:
        until = self._now() + seconds
        if is_global:
            self.limits.global_until = max(self.limits.global_until, until)
        else:
            self.limits.routes[route] = max(self.limits.routes.get(route, 0.0), until)


def reader_from_settings(settings: Settings) -> DiscordRestReader | None:
    """The reader for the bot's own token; None without Discord settings."""
    if settings.discord is None:
        return None
    return DiscordRestReader(settings.discord.bot_token)


def _first(group: BaseExceptionGroup[BaseException]) -> BaseException:
    """The group's first error. The task group runs plain coroutines, so none nests."""
    return group.exceptions[0]


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        seconds = float(cast("str | float", value))
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


# --- payloads to the room rule's inputs -------------------------------------------------


def snapshot_from_payloads(
    guild_id: str,
    *,
    guild: object,
    channels: object,
    threads: object,
    member_pages: Sequence[object],
    now: datetime,
) -> GuildSnapshot:
    """The room rule's inputs from Discord's raw JSON, or `ReadFailed`. Pure.

    Every field the rule needs must be present and well formed. A member's role must be
    in the role list; the owner must be a member; member pages must ascend by id from
    the cursor and the last must be short."""
    body = _dict(guild, "guild")
    if _snowflake(body.get("id"), "guild_id") != guild_id:
        raise ReadFailed("guild_id_mismatch")
    owner_id = _snowflake(body.get("owner_id"), "owner_id")
    role_permissions: dict[str, int] = {}
    for raw in _list(body.get("roles"), "roles"):
        role = _dict(raw, "role")
        role_id = _snowflake(role.get("id"), "role_id")
        if role_id in role_permissions:
            raise ReadFailed("role_duplicate")
        role_permissions[role_id] = _bits(role.get("permissions"), "role_permissions")
    if guild_id not in role_permissions:
        raise ReadFailed("everyone_role_missing")

    rooms = [_channel(raw) for raw in _list(channels, "channels")]
    thread_body = _dict(threads, "threads")
    rooms += [_thread(raw) for raw in _list(thread_body.get("threads"), "threads")]

    members: list[AudienceMember] = []
    seen: set[str] = set()
    after = 0
    for number, page in enumerate(member_pages):
        listed = _list(page, "members")
        if len(listed) > MEMBER_PAGE:
            raise ReadFailed("member_page_too_long")
        if number < len(member_pages) - 1 and len(listed) < MEMBER_PAGE:
            raise ReadFailed("member_page_short")
        for raw in listed:
            member = _member(raw, role_permissions, owner_id=owner_id, now=now)
            if int(member.user_id) <= after or member.user_id in seen:
                raise ReadFailed("member_order")
            after = int(member.user_id)
            seen.add(member.user_id)
            members.append(member)
    if not member_pages or len(_list(member_pages[-1], "members")) >= MEMBER_PAGE:
        raise ReadFailed("member_pages_incomplete")
    if owner_id not in seen:
        raise ReadFailed("owner_not_a_member")
    return GuildSnapshot(
        guild_id=guild_id,
        owner_id=owner_id,
        role_permissions=role_permissions,
        channels=tuple(rooms),
        members=tuple(members),
        member_pages=len(member_pages),
    )


def rooms_from_snapshot(
    snapshot: GuildSnapshot,
    *,
    internal_role_ids: tuple[str, ...],
    team_role_ids: tuple[str, ...],
) -> dict[str, StoredRoom]:
    """Each room's and active thread's facts, with a thread's parent. Pure.

    An archived thread is not in the read: the store gives it its parent's "contained"
    and makes it narrow."""
    facts = compute_room_facts(
        guild_id=snapshot.guild_id,
        members=snapshot.members,
        role_permissions=snapshot.role_permissions,
        channels=snapshot.channels,
        internal_role_ids=internal_role_ids,
        team_role_ids=team_role_ids,
    )
    parents = {c.id: c.parent_id for c in snapshot.channels}
    return {cid: StoredRoom(room, parents.get(cid)) for cid, room in facts.items()}


def _channel(raw: object) -> AudienceChannel:
    channel = _dict(raw, "channel")
    channel_type = _int(channel.get("type"), "channel_type")
    overwrites = tuple(
        _overwrite(o) for o in _list(channel.get("permission_overwrites"), "overwrites")
    )
    return AudienceChannel(
        _snowflake(channel.get("id"), "channel_id"),
        channel_type,
        _str(channel.get("name"), "channel_name"),
        overwrites=overwrites,
    )


def _thread(raw: object) -> AudienceChannel:
    thread = _dict(raw, "thread")
    thread_type = _int(thread.get("type"), "thread_type")
    if thread_type not in _THREAD_TYPES:
        raise ReadFailed("thread_type")
    return AudienceChannel(
        _snowflake(thread.get("id"), "thread_id"),
        thread_type,
        _str(thread.get("name"), "thread_name"),
        parent_id=_snowflake(thread.get("parent_id"), "thread_parent"),
    )


def _overwrite(raw: object) -> DiscordOverwrite:
    overwrite = _dict(raw, "overwrite")
    kind = _int(overwrite.get("type"), "overwrite_type")
    if kind not in (0, 1):
        raise ReadFailed("overwrite_type")
    return DiscordOverwrite(
        _snowflake(overwrite.get("id"), "overwrite_id"),
        kind,
        _bits(overwrite.get("allow"), "overwrite_allow"),
        _bits(overwrite.get("deny"), "overwrite_deny"),
    )


def _member(
    raw: object, role_permissions: Mapping[str, int], *, owner_id: str, now: datetime
) -> AudienceMember:
    member = _dict(raw, "member")
    user = _dict(member.get("user"), "member_user")
    user_id = _snowflake(user.get("id"), "member_id")
    role_ids = frozenset(_snowflake(r, "member_role") for r in _list(member.get("roles"), "roles"))
    if not role_ids <= role_permissions.keys():
        raise ReadFailed("member_role_unresolved")
    is_bot = user.get("bot", False)
    if not isinstance(is_bot, bool):
        raise ReadFailed("member_bot")
    return AudienceMember(
        user_id=user_id,
        role_ids=role_ids,
        is_bot=is_bot,
        is_owner=user_id == owner_id,
        timed_out=_timed_out(member.get("communication_disabled_until"), now),
    )


def _member_id(raw: object) -> str:
    return _snowflake(_dict(_dict(raw, "member").get("user"), "member_user").get("id"), "member_id")


def _timed_out(value: object, now: datetime) -> bool:
    if value is None:
        return False
    try:
        return datetime.fromisoformat(_str(value, "timeout")) > now
    except ValueError:
        raise ReadFailed("timeout") from None


def _dict(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReadFailed(f"{what}_missing")
    return cast("dict[str, Any]", value)


def _list(value: object, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise ReadFailed(f"{what}_missing")
    return cast("list[Any]", value)


def _str(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise ReadFailed(f"{what}_missing")
    return value


def _int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReadFailed(f"{what}_missing")
    return value


def _snowflake(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.isdigit():
        raise ReadFailed(f"{what}_missing")
    return value


def _bits(value: object, what: str) -> int:
    """Discord sends permission bits as a decimal string."""
    if not isinstance(value, str) or not value.isdigit():
        raise ReadFailed(f"{what}_missing")
    return int(value)
