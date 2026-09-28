"""Store for the per-tenant access policy: open when absent, closed when unreadable."""

from __future__ import annotations

import pytest
from daimon.core._models import TenantAccessPolicyRecord
from daimon.core.access_policy import OPEN_ACCESS_POLICY, TenantAccessPolicy
from daimon.core.stores.access_policy import (
    AccessPolicyUnreadable,
    load_access_policy,
    set_access_policy,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def test_tenant_without_a_policy_row_gets_the_open_default(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)

    assert await load_access_policy(db_session, tenant_id=tenant.id) == OPEN_ACCESS_POLICY


async def test_set_then_load_round_trips_and_overwrites(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    first = TenantAccessPolicy(invoker_user_ids=("u1",), sealed_channel_ids=("vault",))
    second = TenantAccessPolicy(protected_channel_ids=("client",))

    await set_access_policy(db_session, tenant_id=tenant.id, policy=first)
    assert await load_access_policy(db_session, tenant_id=tenant.id) == first

    await set_access_policy(db_session, tenant_id=tenant.id, policy=second)
    db_session.expire_all()
    assert await load_access_policy(db_session, tenant_id=tenant.id) == second


async def test_unreadable_row_raises_instead_of_falling_open(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    db_session.add(
        TenantAccessPolicyRecord(tenant_id=tenant.id, policy={"invoker_user_ids": "not-a-list!"})
    )
    await db_session.flush()

    with pytest.raises(AccessPolicyUnreadable):
        await load_access_policy(db_session, tenant_id=tenant.id)
