"""A round lists every installed team's channels and files each under its team."""

from __future__ import annotations

import httpx
from daimon.adapters.teams.channel_listing import list_all_once
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.platform_names import (
    ChannelSighting,
    list_channel_names,
    upsert_channel_names,
)
from daimon.core.stores.teams_installations import record_teams_installation
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _tenant_with_teams(db_session: AsyncSession, teams: dict[str, str]) -> TenantRow:
    tenant = await make_tenant(db_session, platform="teams", workspace_id="entra-1")
    for team_id, name in teams.items():
        await record_teams_installation(
            db_session, tenant_id=tenant.id, team_id=team_id, group_id=f"g-{team_id}", name=name
        )
    await db_session.commit()
    return tenant


async def test_listed_channels_are_stored_under_their_team(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _tenant_with_teams(db_session, {"t1": "Research"})

    async def team_channels(team_id: str) -> dict[str, str]:
        return {team_id: "General", "c1": "Plans"}

    stored = await list_all_once(db_session_factory, team_channels, team_gap_s=0)
    rows = await list_channel_names(db_session, tenant_id=tenant.id, platform="teams")
    assert stored == 2, "both channels are counted"
    assert sorted((r.channel_id, r.name, r.parent_id, r.parent_name) for r in rows) == [
        ("c1", "Plans", "t1", "Research"),
        ("t1", "General", "t1", "Research"),
    ], "General and the other channel sit under the team"


async def test_a_team_that_fails_is_skipped_and_the_others_are_stored(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _tenant_with_teams(db_session, {"t1": "Broken", "t2": "Working"})

    async def team_channels(team_id: str) -> dict[str, str]:
        if team_id == "t1":
            raise httpx.ConnectError("unreachable")
        return {"c2": "Plans"}

    stored = await list_all_once(db_session_factory, team_channels, team_gap_s=0)
    rows = await list_channel_names(db_session, tenant_id=tenant.id, platform="teams")
    assert stored == 1, "only the working team is stored"
    assert [(r.channel_id, r.parent_id) for r in rows] == [("c2", "t2")], "the failure is skipped"


async def test_nothing_is_listed_without_installed_teams(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _tenant_with_teams(db_session, {})
    calls: list[str] = []

    async def team_channels(team_id: str) -> dict[str, str]:
        calls.append(team_id)
        return {}

    assert await list_all_once(db_session_factory, team_channels, team_gap_s=0) == 0
    assert calls == [], "no team, no listing call"


async def test_a_message_sighting_without_a_team_keeps_the_listed_team(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _tenant_with_teams(db_session, {"t1": "Research"})

    async def team_channels(team_id: str) -> dict[str, str]:
        return {team_id: "General"}

    await list_all_once(db_session_factory, team_channels, team_gap_s=0)
    async with db_session_factory.begin() as session:
        await upsert_channel_names(
            session,
            tenant_id=tenant.id,
            platform="teams",
            names={"t1": ChannelSighting("General", kind="standard")},
        )
    rows = await list_channel_names(db_session, tenant_id=tenant.id, platform="teams")
    assert [(r.kind, r.parent_id, r.parent_name) for r in rows] == [
        ("standard", "t1", "Research")
    ], "a sighting with a name but no parent leaves the parent alone"
