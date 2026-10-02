"""Teams team owners as channel admins: grant-named teams only, cached, failing closed."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.adapters.teams.channel_admin_groups import (
    channel_admin_caller,
    fetch_team_owner_ids,
    owned_team_ids,
)
from daimon.core.channel_admins import GroupLookupFailed, GroupMembersCache
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.teams_graph import GraphClient
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TEAM = "0f2c8a51-7d3e-4b9a-8c61-2e5f4a9b7c10"
OWNER = "cde058c4-3357-4ef3-8842-65c5b73974a1"


async def _token() -> str:
    return "graph-token"


async def test_a_refused_owner_read_is_a_failed_lookup() -> None:
    graph = GraphClient(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))), _token
    )
    with pytest.raises(GroupLookupFailed):
        await fetch_team_owner_ids(graph, TEAM)


async def _grant(session: AsyncSession) -> object:
    tenant = await make_tenant(session, platform="teams", workspace_id="entra-tenant")
    await set_channel_admins(
        session,
        tenant_id=tenant.id,
        platform="teams",
        channel_id="19:abc@thread.tacv2",
        role_ids=[TEAM],
        user_ids=[],
        actor_account_id=None,
    )
    await session.commit()
    return tenant.id


async def test_an_owner_of_a_granted_team_holds_it_once_per_ttl(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _grant(db_session)
    asked: list[str] = []

    async def owners(group_id: str) -> frozenset[str]:
        asked.append(group_id)
        return frozenset({OWNER})

    runtime = MagicMock(
        sessionmaker=db_session_factory, team_owners=owners, group_members=GroupMembersCache()
    )

    owner = await owned_team_ids(runtime, tenant_id=tenant_id, user_id=OWNER.upper())  # type: ignore[arg-type]
    member = await channel_admin_caller(
        runtime, tenant_id=tenant_id, user_id="aaaaaaaa-0000-0000-0000-000000000001", is_admin=False
    )  # type: ignore[arg-type]

    assert owner == frozenset({TEAM}), "an owner holds the team; Entra ids compare lower-case"
    assert member.role_ids == frozenset(), "a member who owns nothing holds nothing"
    assert asked == [TEAM], "the second caller is served from the cache"


async def test_without_graph_or_on_a_failed_read_nobody_holds_a_team(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _grant(db_session)

    async def broken(group_id: str) -> frozenset[str]:
        raise GroupLookupFailed("throttled")

    for lookup in (None, broken):
        runtime = MagicMock(
            sessionmaker=db_session_factory, team_owners=lookup, group_members=GroupMembersCache()
        )
        held = await owned_team_ids(runtime, tenant_id=tenant_id, user_id=OWNER)  # type: ignore[arg-type]
        assert held == frozenset(), f"fail closed with {lookup!r}"


async def test_a_turns_owner_lookup_runs_with_no_session_open(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A slow Graph read on every chat turn must not hold a pooled connection."""
    tenant_id = await _grant(db_session)
    open_sessions = [0]
    seen: list[int] = []

    @asynccontextmanager
    async def counting() -> AsyncIterator[AsyncSession]:
        open_sessions[0] += 1
        try:
            async with db_session_factory() as session:
                yield session
        finally:
            open_sessions[0] -= 1

    async def owners(group_id: str) -> frozenset[str]:
        seen.append(open_sessions[0])
        return frozenset({OWNER})

    runtime = MagicMock(
        sessionmaker=counting, team_owners=owners, group_members=GroupMembersCache()
    )
    held = await owned_team_ids(runtime, tenant_id=tenant_id, user_id=OWNER)  # type: ignore[arg-type]

    assert held == frozenset({TEAM}), "the owner still holds the granted team"
    assert seen == [0], "the Graph read ran after the grants session closed"
