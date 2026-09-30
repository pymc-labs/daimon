"""Channel admins: members who administer one channel on top of the server admins.

Server (or workspace) admins administer every channel. A `channel_admins` row
adds role ids and user ids for one channel; no row adds nobody, so a tenant
that never configures one behaves exactly as before. Role ids are the ones the
member held on their last chat turn (`accounts.platform_role_ids`): Discord has
roles, Slack has none, so a Slack grant is by user id only (Discord-only roles,
recorded in tests/parity/test_channel_admin_roles_discord_only.py).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence

from daimon.core.stores.channel_admins import list_channel_admins
from daimon.core.stores.domain import ChannelAdminsRow
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

MAX_CHANNEL_ADMIN_IDS = 25
"""Per list and channel; matches the largest Discord or Slack multi-select."""

_CHANNEL_ID = {"discord": r"[0-9]{15,21}", "slack": r"[CGD][A-Z0-9]+"}
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


async def load_administered_channel_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, caller: ChannelAdminCaller
) -> frozenset[str]:
    """Shell half of `administered_channel_ids`: read this tenant's grants."""
    if caller.platform_user_id is None and not caller.role_ids:
        return frozenset()
    grants = await list_channel_admins(session, tenant_id=tenant_id, platform=platform)
    return administered_channel_ids(caller, grants)


__all__ = [
    "MAX_CHANNEL_ADMIN_IDS",
    "ChannelAdminCaller",
    "InvalidChannelAdminIds",
    "administered_channel_ids",
    "is_channel_admin",
    "load_administered_channel_ids",
    "normalize_channel_admin_ids",
]
