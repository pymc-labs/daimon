"""Teams team owners as channel admin grants: which named teams a member owns.

A grant's group id is a team's Entra group id, and admits that team's owners.
Only teams some grant names are looked up, each through the runtime's short
cache, with the `TeamMember.Read.Group` consent the team owner granted at
install. A team without it, or a failed read, grants nothing.
"""

from __future__ import annotations

import uuid

from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.channel_admins import (
    ChannelAdminCaller,
    GroupLookupFailed,
    load_member_group_ids,
)
from daimon.core.teams_graph import GraphClient, GraphUnavailable


async def fetch_team_owner_ids(graph: GraphClient, group_id: str) -> frozenset[str]:
    """A team's owners; raises `GroupLookupFailed` for any failed Graph read."""
    try:
        return await graph.list_team_owner_ids(group_id)
    except GraphUnavailable as exc:
        raise GroupLookupFailed(exc.reason) from exc


async def owned_team_ids(
    runtime: TeamsRuntime, *, tenant_id: uuid.UUID, user_id: str
) -> frozenset[str]:
    """The teams named by this tenant's grants that `user_id` owns."""
    lookup = runtime.team_owners
    if lookup is None:
        return frozenset()

    async def members(group_id: str) -> frozenset[str]:
        return await runtime.group_members.members(("teams", group_id), lambda: lookup(group_id))

    async with runtime.sessionmaker() as session:
        return await load_member_group_ids(
            session,
            tenant_id=tenant_id,
            platform="teams",
            platform_user_id=user_id.lower(),
            members=members,
        )


async def channel_admin_caller(
    runtime: TeamsRuntime, *, tenant_id: uuid.UUID, user_id: str, is_admin: bool
) -> ChannelAdminCaller:
    """The caller with the grant-named teams they own; a server admin needs none."""
    teams = (
        frozenset[str]()
        if is_admin
        else await owned_team_ids(runtime, tenant_id=tenant_id, user_id=user_id)
    )
    return ChannelAdminCaller(platform_user_id=user_id, role_ids=teams, is_server_admin=is_admin)
