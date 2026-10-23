"""Who administers the deployment from Teams: the admin team's owners.

The owners of `admin_team_id` are platform admins. Authorization reads them
fresh and combines that authority with uncached tenant-wide user grants.
A failed fresh read confers no platform authority. Notification recipient
lookups may use the bounded owner cache and its last successful snapshot.
Every successful owner read also demotes unlisted stored accounts, preserving
explicit Daimon grants; revocation demotes granted accounts separately.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Coroutine
from typing import Any

import structlog
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.provisioning import demote_former_admins
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.config import TeamsSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.native_admins import (
    GroupLookupFailed,
    GroupMembers,
    ServerAdminIds,
    is_daimon_admin,
)
from daimon.core.stores.domain import Role

log = structlog.get_logger()

#: How long the last successful owner read stands in for failed ones.
STALE_OWNERS_S = 3600.0


class AdminTeamReads:
    """The Graph owner read, keeping the admin team's last owners and telling `after_read`.

    It wraps the shared cache read, counting each admin-team owner fetch once.
    A failure is logged here, and a cached one re-raises without a read.
    """

    def __init__(
        self,
        lookup: GroupMembers,
        *,
        team_id: str,
        after_read: Callable[[frozenset[str]], object],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lookup = lookup
        self._team_id = team_id
        self._after_read = after_read
        self._clock = clock
        self._last: tuple[float, frozenset[str]] | None = None

    async def __call__(self, group_id: str) -> frozenset[str]:
        if group_id != self._team_id:
            return await self._lookup(group_id)
        try:
            owners = await self._lookup(group_id)
        except GroupLookupFailed as exc:
            log.warning("teams.admin_team.lookup_failed", reason=str(exc))
            raise
        self._last = (self._clock(), owners)
        self._after_read(owners)
        return owners

    def last_owners(self) -> frozenset[str] | None:
        """The last successful read's owners, or None once it is `STALE_OWNERS_S` old."""
        if self._last is None or self._clock() - self._last[0] >= STALE_OWNERS_S:
            return None
        return self._last[1]


def demoting_owner_reads(
    runtime: TeamsRuntime,
    lookup: GroupMembers,
    *,
    in_background: Callable[[Coroutine[Any, Any, None]], object],
) -> GroupMembers:
    """`lookup`, demoting in the background stored admins dropped from the admin team.

    The stored role is what routines and tools act on: without this, a removed
    owner kept it until their next turn or a restart. A failed read demotes no one.
    """
    settings = runtime.settings.teams
    if settings is None or settings.admin_team_id is None:
        return lookup
    tenant_id = derive_tenant_uuid(platform="teams", workspace_id=settings.tenant_id)

    def demote(owners: frozenset[str]) -> object:
        return in_background(
            demote_former_admins(runtime.sessionmaker, tenant_id=tenant_id, admin_ids=owners)
        )

    return AdminTeamReads(lookup, team_id=settings.admin_team_id, after_read=demote)


def admin_team_owners(runtime: TeamsRuntime, *, fresh: bool = False) -> GroupMembers | None:
    """Live owners through the shared cache, or the last ones for an hour; None without Graph."""
    lookup = runtime.team_owners
    if lookup is None:
        return None
    reads = lookup if isinstance(lookup, AdminTeamReads) else None

    async def owners(group_id: str) -> frozenset[str]:
        try:
            return await runtime.group_members.members(
                ("teams", group_id), lambda: lookup(group_id), fresh=fresh
            )
        except GroupLookupFailed:
            if fresh:
                raise
            # An outage, or the app removed from the team, keeps the admins for an hour.
            last = None if reads is None else reads.last_owners()
            if last is None:
                raise
            return last

    return owners


async def _owner_ids(settings: TeamsSettings, owners: GroupMembers | None) -> frozenset[str]:
    """The admin team's owners; raises `GroupLookupFailed` when they cannot be read."""
    if settings.admin_team_id is None or owners is None:
        return frozenset()
    return await owners(settings.admin_team_id)


async def deployment_role(
    settings: TeamsSettings,
    owners: GroupMembers | None,
    *,
    user_id: str,
) -> Role | None:
    """ADMIN for an owner of the admin team, else USER; None if the owners are unknown.

    None is a user's turn that records no role (`admit`): an outage must not
    write USER over the stored ADMIN that an owner's routines and tools use.
    """
    try:
        owner_ids = await _owner_ids(settings, owners)
    except GroupLookupFailed:
        return None
    return Role.ADMIN if user_id.lower() in owner_ids else Role.USER


async def is_platform_admin(
    settings: TeamsSettings, owners: GroupMembers | None, *, user_id: str
) -> bool:
    """Platform-only authority for billing, caps and delegation."""
    return await deployment_role(settings, owners, user_id=user_id) is Role.ADMIN


async def is_deployment_admin(
    settings: TeamsSettings,
    owners: GroupMembers | None,
    *,
    user_id: str,
    runtime: TeamsRuntime | None = None,
) -> bool:
    """Whether `user_id` has live platform authority or a tenant user grant."""
    role = await deployment_role(settings, owners, user_id=user_id)
    if runtime is None:
        return role is Role.ADMIN
    tenant_id = derive_tenant_uuid(platform="teams", workspace_id=settings.tenant_id)
    async with runtime.sessionmaker() as session:
        return await is_daimon_admin(
            session,
            tenant_id=tenant_id,
            platform="teams",
            platform_user_id=user_id.lower(),
            is_platform_admin=role is Role.ADMIN,
        )


async def inbound_role(
    runtime: TeamsRuntime, settings: TeamsSettings, inbound: TeamsInbound
) -> Role | None:
    """A message sender's `deployment_role`."""
    return await deployment_role(
        settings, admin_team_owners(runtime, fresh=True), user_id=inbound.user_id
    )


async def inbound_is_admin(
    runtime: TeamsRuntime, settings: TeamsSettings, inbound: TeamsInbound
) -> bool:
    """Whether a message's sender administers the deployment."""
    return await is_deployment_admin(
        settings, admin_team_owners(runtime, fresh=True), user_id=inbound.user_id, runtime=runtime
    )


async def deployment_admin_ids(
    settings: TeamsSettings, owners: GroupMembers | None
) -> tuple[str, ...]:
    """Every admin, sorted: the admin team's owners; () for none or unreadable."""
    try:
        return tuple(sorted(await _owner_ids(settings, owners)))
    except GroupLookupFailed:
        return ()  # fail closed


def live_admin_ids(runtime: TeamsRuntime) -> ServerAdminIds:
    """The admins a notice to "the admins" reaches, read at each call."""

    async def admin_ids() -> tuple[str, ...]:
        settings = runtime.settings.teams
        if settings is None:
            return ()
        return await deployment_admin_ids(settings, admin_team_owners(runtime))

    return admin_ids


async def boot_owner_ids(runtime: TeamsRuntime, settings: TeamsSettings) -> frozenset[str] | None:
    """The admin team's owners at boot, for demoting everyone else; None if unreadable."""
    try:
        return await _owner_ids(settings, admin_team_owners(runtime))
    except GroupLookupFailed:
        return None  # demoting on a failed read would take every owner's role
