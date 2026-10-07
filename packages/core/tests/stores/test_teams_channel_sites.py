"""Store tests for Teams channels' granted Files folders."""

from __future__ import annotations

from daimon.core.stores.teams_channel_sites import (
    get_teams_channel_site,
    upsert_teams_channel_site,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

CHANNEL = "19:private@thread.tacv2"


async def test_upsert_replaces_the_folder_and_is_per_tenant(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    for drive in ("d1", "d2"):
        await upsert_teams_channel_site(
            db_session,
            tenant_id=tenant.id,
            channel_id=CHANNEL,
            group_id="g1",
            site_id="host,s1,w1",
            drive_id=drive,
            folder_id="f1",
        )
    row = await get_teams_channel_site(db_session, tenant_id=tenant.id, channel_id=CHANNEL)
    assert row is not None and (row.site_id, row.drive_id) == ("host,s1,w1", "d2"), (
        "a second grant replaces the stored folder"
    )
    assert (
        await get_teams_channel_site(db_session, tenant_id=other.id, channel_id=CHANNEL) is None
    ), "rows are per tenant"
