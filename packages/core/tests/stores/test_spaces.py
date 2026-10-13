"""Store for Spaces: one Space per channel, owner tenant only, cascades."""

from __future__ import annotations

import pytest
from daimon.core.access_policy import ChannelRule
from daimon.core.channel_ref import ChannelRef
from daimon.core.stores import spaces
from daimon.core.stores.accounts import set_external
from daimon.core.stores.spaces import SpaceRefused
from daimon.core.stores.tenants import delete_tenant
from daimon.testing.factories import make_account, make_tenant, make_thread_session
from sqlalchemy.ext.asyncio import AsyncSession

OWN = ChannelRule(readers="own", writers="own")


async def test_space_round_trips_its_channels_and_rule(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    space = await spaces.create_space(db_session, tenant_id=tenant.id, name="client", rule=OWN)
    for channel_id in ("C2", "C1"):
        await spaces.add_space_channel(
            db_session, space=space, tenant_id=tenant.id, channel_id=channel_id
        )

    refs = await spaces.list_space_channels(db_session, tenant_id=tenant.id, space_id=space.id)
    assert refs == [
        ChannelRef(tenant_id=tenant.id, platform="slack", channel_id="C1"),
        ChannelRef(tenant_id=tenant.id, platform="slack", channel_id="C2"),
    ], "members carry their own tenant and platform"
    (rule,) = (await spaces.list_space_rules(db_session, tenant_id=tenant.id)).values()
    assert (rule.key, rule.name, rule.rule, rule.channel_ids) == (
        spaces.space_key(space.id),
        "client",
        OWN,
        frozenset({"C1", "C2"}),
    ), "the pure view names the Space by its key"
    assert await spaces.remove_space_channel(
        db_session, space_id=space.id, tenant_id=tenant.id, channel_id="C1"
    ), "removing a member reports it"
    assert await spaces.delete_space(db_session, tenant_id=tenant.id, space_id=space.id), (
        "deleting reports it"
    )
    assert await spaces.list_space_rules(db_session, tenant_id=tenant.id) == {}, "nothing left"


async def test_a_channel_joins_one_space_of_its_own_tenant(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, workspace_id="g1")
    other = await make_tenant(db_session, workspace_id="g2")
    first = await spaces.create_space(db_session, tenant_id=tenant.id, name="a", rule=OWN)
    second = await spaces.create_space(db_session, tenant_id=tenant.id, name="b", rule=OWN)
    await spaces.add_space_channel(db_session, space=first, tenant_id=tenant.id, channel_id="1")

    with pytest.raises(SpaceRefused, match="already in a Space"):
        await spaces.add_space_channel(
            db_session, space=second, tenant_id=tenant.id, channel_id="1"
        )
    with pytest.raises(SpaceRefused, match="already in Space a"):
        await spaces.add_space_channel(db_session, space=first, tenant_id=tenant.id, channel_id="1")
    assert (
        await spaces.list_space_channels(db_session, tenant_id=other.id, space_id=first.id) == []
    ), "another tenant reads none of its channels"
    assert not await spaces.delete_space(db_session, tenant_id=other.id, space_id=first.id), (
        "nor deletes it"
    )
    with pytest.raises(SpaceRefused, match="owns it"):
        await spaces.add_space_channel(db_session, space=first, tenant_id=other.id, channel_id="9")
    with pytest.raises(SpaceRefused, match="already a Space named a"):
        await spaces.create_space(db_session, tenant_id=tenant.id, name="a", rule=OWN)


async def test_tenant_delete_cascades_its_spaces(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    space = await spaces.create_space(db_session, tenant_id=tenant.id, name="a", rule=OWN)
    await spaces.add_space_channel(db_session, space=space, tenant_id=tenant.id, channel_id="1")

    await delete_tenant(db_session, tenant_id=tenant.id)

    assert (
        await spaces.list_space_channels(db_session, tenant_id=tenant.id, space_id=space.id) == []
    ), "cascaded"


async def test_external_accounts_are_counted_per_channel(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="teams")
    guest = await make_account(db_session, tenant=tenant)
    await set_external(db_session, guest.id, True)
    for thread in ("t1", "t2"):
        await make_thread_session(
            db_session, tenant=tenant, account=guest, channel_id="shared", thread_id=thread
        )
    await make_thread_session(db_session, tenant=tenant, channel_id="staff")

    seen = await spaces.count_external_accounts_seen(
        db_session, tenant_id=tenant.id, channel_ids=["shared", "staff"]
    )
    assert seen == {"shared": 1, "staff": 0}, "one guest, counted once, only where they talked"
