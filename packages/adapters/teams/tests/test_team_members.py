"""Teams team membership for DM grants: failures deny, the cache serves delivery."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from daimon.adapters.teams.team_owners import fetch_team_member_ids, team_member_lookup
from daimon.core.native_admins import GroupLookupFailed, GroupMembersCache
from daimon.core.teams_graph import GraphUnavailable


class _Graph:
    def __init__(self, error: GraphUnavailable | None = None) -> None:
        self.error = error

    async def list_team_member_ids(self, group_id: str) -> frozenset[str]:
        if self.error is not None:
            raise self.error
        return frozenset({"u1"})


async def test_fetch_team_member_ids_when_graph_refuses_then_lookup_failed() -> None:
    graph: Any = _Graph(GraphUnavailable("forbidden", status=403))
    with pytest.raises(GroupLookupFailed, match=r"forbidden \(status 403\)"):
        await fetch_team_member_ids(graph, "g1")
    ok: Any = _Graph()
    assert await fetch_team_member_ids(ok, "g1") == frozenset({"u1"}), "a team's members"


async def test_team_member_lookup_when_no_graph_then_none_else_cached_unless_fresh() -> None:
    calls: list[str] = []

    async def fetch(group_id: str) -> frozenset[str]:
        calls.append(group_id)
        return frozenset({"u1"})

    bare: Any = SimpleNamespace(team_members=None, group_members=GroupMembersCache())
    assert team_member_lookup(bare) is None, "no Graph wired: group grants fail closed"
    runtime: Any = SimpleNamespace(team_members=fetch, group_members=GroupMembersCache())
    cached = team_member_lookup(runtime)
    assert cached is not None, "Graph wired"
    await cached("g1")
    await cached("g1")
    assert calls == ["g1"], "delivery reuses the cached roster"
    fresh = team_member_lookup(runtime, fresh=True)
    assert fresh is not None, "Graph wired"
    await fresh("g1")
    assert calls == ["g1", "g1"], "a conversation's start looks up afresh"
