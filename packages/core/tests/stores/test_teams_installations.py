"""Store tests for the teams the Teams bot is installed in."""

from __future__ import annotations

from daimon.core.stores.teams_installations import (
    delete_teams_installation,
    get_teams_installation,
    list_teams_installations,
    record_teams_installation,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

TEAM = "19:team@thread.tacv2"


async def test_record_upserts_and_keeps_a_known_name(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    assert await record_teams_installation(
        db_session, tenant_id=tenant.id, team_id=TEAM, group_id="g1", name="Research"
    ), "the first sighting is new"
    assert not await record_teams_installation(
        db_session, tenant_id=tenant.id, team_id=TEAM, group_id="g2", name=None
    ), "a later sighting is not"
    row = await get_teams_installation(db_session, tenant_id=tenant.id, team_id=TEAM)
    assert row is not None and (row.group_id, row.name) == ("g2", "Research"), (
        "the group follows the latest sighting; a missing name keeps the stored one"
    )
    assert await list_teams_installations(db_session, tenant_id=other.id) == [], "per tenant"
    await delete_teams_installation(db_session, tenant_id=tenant.id, team_id=TEAM)
    assert await list_teams_installations(db_session, tenant_id=tenant.id) == []
