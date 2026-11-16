"""Live Teams team lookups: owners for native admin checks, members for DM grants."""

from __future__ import annotations

from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.native_admins import GroupLookupFailed, GroupMembers
from daimon.core.teams_graph import GraphClient, GraphUnavailable


async def fetch_team_owner_ids(graph: GraphClient, group_id: str) -> frozenset[str]:
    """A team's owners; raises `GroupLookupFailed` (reason and status) for a failed read."""
    try:
        return await graph.list_team_owner_ids(group_id)
    except GraphUnavailable as exc:
        status = "" if exc.status is None else f" (status {exc.status})"
        raise GroupLookupFailed(f"{exc.reason}{status}") from exc


async def fetch_team_member_ids(graph: GraphClient, group_id: str) -> frozenset[str]:
    """Everyone in a team; raises `GroupLookupFailed` (reason and status) for a failed read."""
    try:
        return await graph.list_team_member_ids(group_id)
    except GraphUnavailable as exc:
        status = "" if exc.status is None else f" (status {exc.status})"
        raise GroupLookupFailed(f"{exc.reason}{status}") from exc


def team_member_lookup(runtime: TeamsRuntime, *, fresh: bool = False) -> GroupMembers | None:
    """A team's live members, through the runtime's short cache; None without Graph."""
    lookup = runtime.team_members
    if lookup is None:
        return None

    async def members(group_id: str) -> frozenset[str]:
        return await runtime.group_members.members(
            ("teams", "members", group_id), lambda: lookup(group_id), fresh=fresh
        )

    return members


def team_owner_lookup(runtime: TeamsRuntime, *, fresh: bool = False) -> GroupMembers | None:
    """A team's live owners, through the runtime's short cache; None without Graph."""
    lookup = runtime.team_owners
    if lookup is None:
        return None

    async def members(group_id: str) -> frozenset[str]:
        return await runtime.group_members.members(
            ("teams", group_id), lambda: lookup(group_id), fresh=fresh
        )

    return members
