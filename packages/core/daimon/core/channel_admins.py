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
an MCP call. Recorded in tests/parity/test_channel_admin_groups.py.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Final

import structlog
from daimon.core.authz import Subject, build_subject
from daimon.core.errors import DaimonError
from daimon.core.stores.accounts import get_account
from daimon.core.stores.channel_admins import list_channel_admins
from daimon.core.stores.domain import ChannelAdminsRow, Role
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

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


class GroupMembersCache:
    """Group members by key, kept `ttl_s` seconds, so a busy channel costs one lookup a minute.

    A failed lookup is not kept: the next caller asks again. ``clock`` is
    injected for tests.
    """

    def __init__(
        self,
        *,
        ttl_s: float = GROUP_MEMBERS_TTL_S,
        max_entries: int = 1_024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[tuple[str, ...], tuple[float, frozenset[str]]] = {}

    async def members(
        self, key: tuple[str, ...], fetch: Callable[[], Awaitable[frozenset[str]]]
    ) -> frozenset[str]:
        now = self._clock()
        cached = self._entries.get(key)
        if cached is not None and now - cached[0] < self._ttl_s:
            return cached[1]
        members = await fetch()
        if len(self._entries) >= self._max_entries:
            self._entries = {k: v for k, v in self._entries.items() if now - v[0] < self._ttl_s}
            if len(self._entries) >= self._max_entries:
                self._entries.clear()
        self._entries[key] = (now, members)
        return members


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
    named = {group_id for grant in grants for group_id in grant.role_ids}
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
) -> Subject:
    """The caller as their stored role and grants describe them, as of their last chat turn.

    For callers with no live platform role: a private form's submit and a hub
    login. A server admin's grants are not read; they need none.
    """
    account = await get_account(session, account_id)
    if account is None or account.role is Role.ADMIN:
        return build_subject(is_admin=account is not None, platform_user_id=platform_user_id)
    return build_subject(
        is_admin=False,
        platform_user_id=platform_user_id,
        administered_channel_ids=await load_administered_channel_ids(
            session,
            tenant_id=tenant_id,
            platform=platform or "",
            caller=ChannelAdminCaller(
                platform_user_id=platform_user_id, role_ids=frozenset(account.platform_role_ids)
            ),
        ),
    )


__all__ = [
    "CHANNEL_ADMIN_PLATFORMS",
    "GROUP_MEMBERS_TTL_S",
    "MAX_CHANNEL_ADMIN_IDS",
    "MAX_LISTED_MENTIONS",
    "ChannelAdminCaller",
    "GroupLookupFailed",
    "GroupMembers",
    "GroupMembersCache",
    "InvalidChannelAdminIds",
    "administered_channel_ids",
    "fit_lines",
    "fold_mentions",
    "is_channel_admin",
    "load_administered_channel_ids",
    "load_live_subject",
    "load_member_group_ids",
    "member_group_ids",
    "load_stored_subject",
    "normalize_channel_admin_ids",
]
