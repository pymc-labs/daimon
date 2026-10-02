"""Channel admins: members who administer one channel on top of the server admins.

Server (or workspace) admins administer every channel. A `channel_admins` row
adds role ids and user ids for one channel; no row adds nobody, so a tenant
that never configures one behaves exactly as before. Role ids are the ones the
member held on their last chat turn (`accounts.platform_role_ids`): Discord has
roles, Slack has none, so a Slack grant is by user id only (Discord-only roles,
recorded in tests/parity/test_channel_admin_roles_discord_only.py). Teams has
no channel admins (tests/parity/test_teams_deliberate_gaps.py).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from typing import Final

from daimon.core.authz import Subject, build_subject
from daimon.core.stores.accounts import get_account
from daimon.core.stores.channel_admins import list_channel_admins
from daimon.core.stores.domain import ChannelAdminsRow, Role
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

CHANNEL_ADMIN_PLATFORMS: Final = ("discord", "slack")
"""The platforms a channel can have its own admins on."""

MAX_CHANNEL_ADMIN_IDS = 25
"""Per list and channel; matches the largest Discord or Slack multi-select."""

# Slack DM ids (`D...`) are refused: nobody administers a DM.
_CHANNEL_ID = {"discord": r"[0-9]{15,21}", "slack": r"[CG][A-Z0-9]+"}
_USER_ID = {"discord": r"[0-9]{15,21}", "slack": r"[UW][A-Z0-9]+"}
_ROLE_ID = {"discord": r"[0-9]{15,21}"}


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

    Raises `InvalidChannelAdminIds` naming the first bad id. Slack has no
    roles, so any Slack role id is refused rather than stored and never matched.
    """
    if platform not in _CHANNEL_ID:
        raise InvalidChannelAdminIds(f"channel admins are not supported on {platform!r}")
    channel = channel_id.strip()
    if re.fullmatch(_CHANNEL_ID[platform], channel) is None:
        raise InvalidChannelAdminIds(f"invalid {platform} channel id {channel_id!r}")
    roles = tuple(dict.fromkeys(value.strip() for value in role_ids))
    users = tuple(dict.fromkeys(value.strip() for value in user_ids))
    if roles and platform not in _ROLE_ID:
        raise InvalidChannelAdminIds(f"{platform} has no roles; grant channel admin by user")
    for kind, ids, pattern in (
        ("role", roles, _ROLE_ID.get(platform, "")),
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
    "MAX_CHANNEL_ADMIN_IDS",
    "MAX_LISTED_MENTIONS",
    "ChannelAdminCaller",
    "InvalidChannelAdminIds",
    "administered_channel_ids",
    "fit_lines",
    "fold_mentions",
    "is_channel_admin",
    "load_administered_channel_ids",
    "load_live_subject",
    "load_stored_subject",
    "normalize_channel_admin_ids",
]
