"""Channel admins: members who administer one channel on top of the server admins.

Server (or workspace) admins administer every channel. A `channel_admins` row
adds group ids (`role_ids`) and user ids for one channel; no row adds nobody, so
a tenant that never configures one behaves exactly as before. A group is a
Discord role, a Slack user group, or a Teams team, whose owners it admits. A
Teams user id is the member's Entra object id.

Discord sends a member's roles with every event. Slack and Teams don't, so their
adapters look up only the groups some grant names (`load_member_group_ids`),
each through a short cache (`GroupMembersCache`); a lookup that fails grants
nothing. Either way the ids a chat turn matched are kept
(`accounts.platform_role_ids`) for callers with no live platform view, such as
an MCP call. Slack lets any member edit a user group by default, so outside a
turn a stored Slack group, and likewise a Teams team, counts only while a live
lookup still admits the person (`confirm_stored_group_ids`); with no lookup it
counts for nothing. Recorded in tests/parity/test_channel_admin_groups.py.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Collection, Iterable, Sequence
from typing import Final

import structlog
from daimon.core.authz import Subject, build_subject
from daimon.core.errors import DaimonError
from daimon.core.stores.accounts import get_account, list_platform_user_ids
from daimon.core.stores.channel_admins import get_channel_admins, list_channel_admins
from daimon.core.stores.domain import ChannelAdminsRow, Role
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CHANNEL_ADMIN_PLATFORMS: Final = ("discord", "slack", "teams")
"""The platforms a channel can have its own admins on."""

MAX_CHANNEL_ADMIN_IDS = 25
"""Per list and channel; matches the largest Discord or Slack multi-select."""

# Slack DM ids (`D...`) are refused: nobody administers a DM.
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_CHANNEL_ID = {
    "discord": r"[0-9]{15,21}",
    "slack": r"[CG][A-Z0-9]+",
    "teams": r"19:[\w-]+@thread\.(?:tacv2|skype)",
}
_USER_ID = {"discord": r"[0-9]{15,21}", "slack": r"[UW][A-Z0-9]+", "teams": _UUID}
# A Discord role, a Slack user group, or a Teams team's Entra group id.
_ROLE_ID = {"discord": r"[0-9]{15,21}", "slack": r"S[A-Z0-9]+", "teams": _UUID}

GROUP_MEMBERS_TTL_S: Final = 60.0
"""How long a Slack user group's or a Teams team's looked-up members are trusted."""

GROUP_LOOKUP_FAILURE_TTL_S: Final = 15.0
"""How long a failed group lookup is remembered before the platform is asked again."""

LOOKED_UP_GROUP_PLATFORMS: Final = frozenset({"slack", "teams"})
"""Platforms whose groups are looked up, not sent: stored ones are re-checked live."""

_log = structlog.get_logger(__name__)


class InvalidChannelAdminIds(ValueError):
    """A channel, role or user id that is not the platform's own id format."""


class ChannelAdminCaller(BaseModel):
    """Who is asking, as far as channel admin grants are concerned."""

    model_config = ConfigDict(frozen=True)

    platform_user_id: str | None
    role_ids: frozenset[str] = frozenset()
    is_server_admin: bool = False


def is_channel_admin(caller: ChannelAdminCaller, *, grant: ChannelAdminsRow | None) -> bool:
    """Server admin, or the channel's grant lists the caller's user id or one of their roles."""
    if caller.is_server_admin:
        return True
    if grant is None:
        return False
    if caller.platform_user_id is not None and caller.platform_user_id in grant.user_ids:
        return True
    return not caller.role_ids.isdisjoint(grant.role_ids)


def administered_channel_ids(
    caller: ChannelAdminCaller, grants: Iterable[ChannelAdminsRow]
) -> frozenset[str]:
    """The channels whose grant names the caller. A server admin needs no grant."""
    return frozenset(
        grant.channel_id
        for grant in grants
        if is_channel_admin(caller.model_copy(update={"is_server_admin": False}), grant=grant)
    )


def normalize_channel_admin_ids(
    platform: str,
    *,
    channel_id: str,
    role_ids: Sequence[str],
    user_ids: Sequence[str],
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Strip, de-duplicate and check every id against the platform's format.

    Raises `InvalidChannelAdminIds` naming the first bad id. Group ids are
    the platform's own: a Discord role, a Slack user group (`S...`) or a
    Teams team's Entra group id.
    """
    if platform not in _CHANNEL_ID:
        raise InvalidChannelAdminIds(f"channel admins are not supported on {platform!r}")
    channel = channel_id.strip()
    if re.fullmatch(_CHANNEL_ID[platform], channel) is None:
        raise InvalidChannelAdminIds(f"invalid {platform} channel id {channel_id!r}")
    # Entra object ids compare case-insensitively; Teams sends them lower-case.
    fold = str.lower if platform == "teams" else str
    roles = tuple(dict.fromkeys(fold(value.strip()) for value in role_ids))
    users = tuple(dict.fromkeys(fold(value.strip()) for value in user_ids))
    for kind, ids, pattern in (
        ("role" if platform == "discord" else "group", roles, _ROLE_ID[platform]),
        ("user", users, _USER_ID[platform]),
    ):
        if len(ids) > MAX_CHANNEL_ADMIN_IDS:
            raise InvalidChannelAdminIds(f"at most {MAX_CHANNEL_ADMIN_IDS} {kind} ids per channel")
        for value in ids:
            if re.fullmatch(pattern, value) is None:
                raise InvalidChannelAdminIds(f"invalid {platform} {kind} id {value!r}")
    return channel, roles, users


MAX_LISTED_MENTIONS = 5
"""Mentions shown per channel in a listing; the rest fold into "+N more"."""


def fold_mentions(mentions: Sequence[str]) -> str:
    """The first `MAX_LISTED_MENTIONS` mentions, comma-joined, then "+N more"."""
    shown = ", ".join(mentions[:MAX_LISTED_MENTIONS])
    rest = len(mentions) - MAX_LISTED_MENTIONS
    return f"{shown} +{rest} more" if rest > 0 else shown


def fit_lines(lines: Iterable[str], *, max_chars: int) -> list[str]:
    """The leading `lines` whose newline-joined text stays within `max_chars`."""
    kept: list[str] = []
    used = -1
    for line in lines:
        used += len(line) + 1
        if used > max_chars:
            break
        kept.append(line)
    return kept


class GroupLookupFailed(DaimonError):
    """A group's members could not be read; the group then grants nothing."""


GroupMembers = Callable[[str], Awaitable[frozenset[str]]]
"""A group id to the user ids it admits; raises `GroupLookupFailed`."""

GroupMembersFor = Callable[[str, str], GroupMembers | None]
"""A platform and workspace id to that workspace's group lookup, or None without one."""


class GroupMembersCache:
    """Group members by key, kept `ttl_s` seconds, so a busy channel costs one lookup a minute.

    A failed lookup is kept `failure_ttl_s` seconds and callers asking for one
    key at once share a single lookup, so a group the platform refuses (a
    missing scope, a rate limit) is not asked again by every request, and a
    burst of requests can't spend the app's rate limit for every other group
    admin. ``clock`` is injected for tests.
    """

    def __init__(
        self,
        *,
        ttl_s: float = GROUP_MEMBERS_TTL_S,
        failure_ttl_s: float = GROUP_LOOKUP_FAILURE_TTL_S,
        max_entries: int = 1_024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._failure_ttl_s = failure_ttl_s
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[tuple[str, ...], tuple[float, frozenset[str]]] = {}
        self._failures: dict[tuple[str, ...], tuple[float, str]] = {}
        self._in_flight: dict[tuple[str, ...], asyncio.Task[frozenset[str]]] = {}

    async def members(
        self, key: tuple[str, ...], fetch: Callable[[], Awaitable[frozenset[str]]]
    ) -> frozenset[str]:
        now = self._clock()
        cached = self._entries.get(key)
        if cached is not None and now - cached[0] < self._ttl_s:
            return cached[1]
        failed = self._failures.get(key)
        if failed is not None and now - failed[0] < self._failure_ttl_s:
            raise GroupLookupFailed(f"{failed[1]} (recent failure)")
        task = self._in_flight.get(key)
        if task is None or task.get_loop() is not asyncio.get_running_loop():
            task = asyncio.create_task(self._fetch(key, fetch), name="channel_admins.group_lookup")
            self._in_flight[key] = task
            task.add_done_callback(lambda done: self._forget(key, done))
        # Shielded: one caller giving up must not cancel the lookup the others await.
        return await asyncio.shield(task)

    async def _fetch(
        self, key: tuple[str, ...], fetch: Callable[[], Awaitable[frozenset[str]]]
    ) -> frozenset[str]:
        try:
            members = await fetch()
        except GroupLookupFailed as exc:
            self._put(self._failures, key, str(exc), ttl_s=self._failure_ttl_s)
            raise
        self._failures.pop(key, None)
        self._put(self._entries, key, members, ttl_s=self._ttl_s)
        return members

    def _put[V](
        self,
        store: dict[tuple[str, ...], tuple[float, V]],
        key: tuple[str, ...],
        value: V,
        *,
        ttl_s: float,
    ) -> None:
        now = self._clock()
        if len(store) >= self._max_entries:
            live = {k: v for k, v in store.items() if now - v[0] < ttl_s}
            store.clear()
            if len(live) < self._max_entries:
                store.update(live)
        store[key] = (now, value)

    def _forget(self, key: tuple[str, ...], done: asyncio.Task[frozenset[str]]) -> None:
        if self._in_flight.get(key) is done:
            del self._in_flight[key]
        if not done.cancelled():
            done.exception()  # retrieved, so a lookup nobody awaits any more logs nothing


async def member_group_ids(
    platform_user_id: str, group_ids: Iterable[str], members: GroupMembers
) -> frozenset[str]:
    """The groups among `group_ids` that admit the user. A failed lookup grants nothing."""
    matched: set[str] = set()
    for group_id in sorted(set(group_ids)):
        try:
            if platform_user_id in await members(group_id):
                matched.add(group_id)
        except GroupLookupFailed as exc:
            _log.warning("channel_admins.group_lookup_failed", group_id=group_id, reason=str(exc))
    return frozenset(matched)


def grant_group_ids(grants: Iterable[ChannelAdminsRow]) -> frozenset[str]:
    """Every group id some grant names."""
    return frozenset(group_id for grant in grants for group_id in grant.role_ids)


async def confirm_stored_group_ids(
    platform: str | None,
    platform_user_id: str | None,
    stored_ids: Iterable[str],
    members: GroupMembers | None,
    *,
    named: Collection[str],
) -> frozenset[str]:
    """The stored group ids that still admit the person, for a caller outside a chat turn.

    Discord sends roles with every event and guards them with Manage Roles, so
    its stored roles stand. A Slack user group or Teams team is looked up again
    (`members`, cached), so someone who left it, or added themselves where
    members may edit groups, counts as they are now. Only the groups some
    grant still names (`named`) are looked up; the rest grant nothing anyway.
    No lookup grants nothing.
    """
    if platform not in LOOKED_UP_GROUP_PLATFORMS:
        return frozenset(stored_ids)
    if members is None or platform_user_id is None:
        return frozenset()
    user_id = platform_user_id.lower() if platform == "teams" else platform_user_id
    return await member_group_ids(user_id, (g for g in stored_ids if g in named), members)


async def load_member_group_ids(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    members: GroupMembers,
) -> frozenset[str]:
    """The groups some grant of this tenant names that admit the user: Slack and Teams.

    Only grant-named groups are looked up, so a tenant with no group grant
    makes no lookup at all.
    """
    grants = await list_channel_admins(session, tenant_id=tenant_id, platform=platform)
    named = grant_group_ids(grants)
    if not named:
        return frozenset()
    return await member_group_ids(platform_user_id, named, members)


async def load_administered_channel_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, caller: ChannelAdminCaller
) -> frozenset[str]:
    """Shell half of `administered_channel_ids`: read this tenant's grants."""
    if platform not in CHANNEL_ADMIN_PLATFORMS or (
        caller.platform_user_id is None and not caller.role_ids
    ):
        return frozenset()
    grants = await list_channel_admins(session, tenant_id=tenant_id, platform=platform)
    return administered_channel_ids(caller, grants)


async def load_live_subject(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, caller: ChannelAdminCaller
) -> Subject:
    """The caller as their live platform role and roles describe them.

    For a panel click, where the platform says who the caller is right now. A
    server admin's grants are not read; they need none.
    """
    if caller.is_server_admin:
        return build_subject(is_admin=True, platform_user_id=caller.platform_user_id)
    return build_subject(
        is_admin=False,
        platform_user_id=caller.platform_user_id,
        administered_channel_ids=await load_administered_channel_ids(
            session, tenant_id=tenant_id, platform=platform, caller=caller
        ),
    )


async def load_stored_subject(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str | None,
    account_id: uuid.UUID,
    platform_user_id: str | None,
    members: GroupMembers | None = None,
) -> Subject:
    """The caller as their stored role and grants describe them, as of their last chat turn.

    For callers with no live platform role: a private form's submit and a hub
    login. A server admin's grants are not read; they need none. Stored Slack
    groups and Teams teams count only as `members` confirms them now.
    """
    account = await get_account(session, account_id)
    if account is None or account.role is Role.ADMIN:
        return build_subject(is_admin=account is not None, platform_user_id=platform_user_id)
    grants = (
        await list_channel_admins(session, tenant_id=tenant_id, platform=platform)
        if platform in CHANNEL_ADMIN_PLATFORMS
        else []
    )
    role_ids = await confirm_stored_group_ids(
        platform,
        platform_user_id,
        account.platform_role_ids,
        members,
        named=grant_group_ids(grants),
    )
    caller = ChannelAdminCaller(platform_user_id=platform_user_id, role_ids=role_ids)
    return build_subject(
        is_admin=False,
        platform_user_id=platform_user_id,
        administered_channel_ids=administered_channel_ids(caller, grants),
    )


async def live_group_member_ids(
    group_ids: Iterable[str], members: GroupMembers | None
) -> frozenset[str]:
    """Everyone the groups admit now; a failed lookup, or none at all, adds nobody."""
    found: set[str] = set()
    if members is None:
        return frozenset()
    for group_id in sorted(set(group_ids)):
        try:
            found |= await members(group_id)
        except GroupLookupFailed as exc:
            _log.warning("channel_admins.group_lookup_failed", group_id=group_id, reason=str(exc))
    return frozenset(found)


async def channel_admin_user_ids(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    limit: int,
    members: GroupMembers | None = None,
) -> list[str] | None:
    """Who a message to `channel_id`'s admins reaches, or None when it has no grant.

    The grant's users, plus members whose stored roles or groups match a
    granted one, as of their last chat turn; a Slack group or Teams team match
    must still hold by `members` now. Sorted, at most `limit`, counted after
    that re-check: capped before it, members who left could fill the cap.
    Reads in a session of its own, closed before the lookup, so a slow
    platform never holds a pooled connection.
    """
    looked_up = platform in LOOKED_UP_GROUP_PLATFORMS
    async with sessionmaker() as session:
        grant = await get_channel_admins(
            session, tenant_id=tenant_id, platform=platform, channel_id=channel_id
        )
        if grant is None or not (grant.user_ids or grant.role_ids):
            return None
        found = await list_platform_user_ids(
            session,
            tenant_id=tenant_id,
            platform=platform,
            limit=None if looked_up else limit,
            user_ids=grant.user_ids,
            role_ids=grant.role_ids,
        )
    if looked_up:
        live = await live_group_member_ids(grant.role_ids, members)
        fold = str.lower if platform == "teams" else str
        found = [uid for uid in found if uid in grant.user_ids or fold(uid) in live]
    # A granted user who never spoke to the bot has no account yet.
    return sorted({*found, *grant.user_ids})[:limit]


__all__ = [
    "CHANNEL_ADMIN_PLATFORMS",
    "GROUP_LOOKUP_FAILURE_TTL_S",
    "GROUP_MEMBERS_TTL_S",
    "LOOKED_UP_GROUP_PLATFORMS",
    "MAX_CHANNEL_ADMIN_IDS",
    "MAX_LISTED_MENTIONS",
    "ChannelAdminCaller",
    "GroupLookupFailed",
    "GroupMembers",
    "GroupMembersCache",
    "GroupMembersFor",
    "InvalidChannelAdminIds",
    "administered_channel_ids",
    "channel_admin_user_ids",
    "confirm_stored_group_ids",
    "fit_lines",
    "fold_mentions",
    "grant_group_ids",
    "is_channel_admin",
    "live_group_member_ids",
    "load_administered_channel_ids",
    "load_live_subject",
    "load_member_group_ids",
    "member_group_ids",
    "load_stored_subject",
    "normalize_channel_admin_ids",
]
