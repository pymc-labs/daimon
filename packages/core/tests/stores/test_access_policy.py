"""Store for the per-tenant access policy: open when absent, closed when unreadable."""

from __future__ import annotations

import pytest
from daimon.core.access_policy import OPEN_ACCESS_POLICY, TenantAccessPolicy
from daimon.core.stores.access_policy import (
    AccessPolicyUnreadable,
    clear_access_policy,
    load_access_policy,
    set_access_policy,
)
from daimon.testing.factories import make_tenant
from sqlalchemy import text
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


@pytest.mark.parametrize(
    "stored",
    ['{"invoker_user_ids": "not-a-list!"}', '{"typo_ids": []}', "null", "[]", '"open"'],
    ids=["bad-field", "unknown-field", "json-null", "array", "string"],
)
async def test_unreadable_row_raises_instead_of_falling_open(
    db_session: AsyncSession, stored: str
) -> None:
    """Only an absent row is open; a present row that isn't a valid policy object
    -- JSON null included -- fails closed."""
    tenant = await make_tenant(db_session)
    await db_session.execute(
        text(
            "INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:t, CAST(:p AS jsonb))"
        ),
        {"t": tenant.id, "p": stored},
    )

    with pytest.raises(AccessPolicyUnreadable):
        await load_access_policy(db_session, tenant_id=tenant.id)


async def test_clear_removes_the_row_and_reports_whether_one_existed(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session, tenant_id=tenant.id, policy=TenantAccessPolicy(invoker_user_ids=("u1",))
    )

    assert await clear_access_policy(db_session, tenant_id=tenant.id) is True
    assert await clear_access_policy(db_session, tenant_id=tenant.id) is False, "nothing left"
    assert await load_access_policy(db_session, tenant_id=tenant.id) == OPEN_ACCESS_POLICY


async def test_an_empty_pin_map_is_left_out_of_the_stored_row(db_session: AsyncSession) -> None:
    """A process built before pins existed must still read the row."""
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session, tenant_id=tenant.id, policy=TenantAccessPolicy(sealed_channel_ids=("c1",))
    )
    stored = (
        await db_session.execute(
            text("SELECT policy FROM tenant_access_policies WHERE tenant_id = :t"),
            {"t": tenant.id},
        )
    ).scalar_one()
    assert "agent_channel_pins" not in stored
    assert await load_access_policy(db_session, tenant_id=tenant.id) == TenantAccessPolicy(
        sealed_channel_ids=("c1",)
    )


async def test_a_row_with_the_old_member_guest_list_still_loads(db_session: AsyncSession) -> None:
    """Earlier builds stored a Teams guest list; guests are members now, so it is dropped."""
    tenant = await make_tenant(db_session)
    await db_session.execute(
        text(
            "INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:t, CAST(:p AS jsonb))"
        ),
        {"t": tenant.id, "p": '{"sealed_channel_ids": ["c1"], "member_guest_ids": ["g1"]}'},
    )

    assert await load_access_policy(db_session, tenant_id=tenant.id) == TenantAccessPolicy(
        sealed_channel_ids=("c1",)
    ), "the guest list is ignored, the rest kept"


@pytest.mark.parametrize("field", ["invoker_role_ids", "internal_role_ids"])
async def test_discord_role_policy_roundtrip_and_everyone_rejection(
    db_session: AsyncSession,
    field: str,
) -> None:
    tenant = await make_tenant(db_session, workspace_id="111")
    policy = TenantAccessPolicy.model_validate({field: ["222"]})
    await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    db_session.expire_all()
    assert await load_access_policy(db_session, tenant_id=tenant.id) == policy
    with pytest.raises(ValueError, match="@everyone"):
        await set_access_policy(
            db_session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy.model_validate({field: ["111"]}),
        )


@pytest.mark.parametrize("platform", ["slack", "teams"])
@pytest.mark.parametrize("field", ["invoker_role_ids", "internal_role_ids"])
async def test_other_platforms_reject_role_writes_and_invalid_rows(
    db_session: AsyncSession, platform: str, field: str
) -> None:
    tenant = await make_tenant(db_session, platform=platform)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Discord"):
        await set_access_policy(
            db_session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy.model_validate({field: ["222"]}),
        )
    await db_session.execute(
        text(
            "INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:t, CAST(:p AS jsonb))"
        ),
        {"t": tenant.id, "p": '{"' + field + '": ["222"]}'},
    )
    with pytest.raises(AccessPolicyUnreadable):
        await load_access_policy(db_session, tenant_id=tenant.id)


async def test_admin_bypass_old_row_and_durable_switch(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await db_session.execute(
        text(
            "INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:t, CAST(:p AS jsonb))"
        ),
        {"t": tenant.id, "p": '{"invoker_user_ids": ["u1"]}'},
    )
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.invoker_admin_bypass
    updated = policy.model_copy(update={"invoker_admin_bypass": False})
    await set_access_policy(db_session, tenant_id=tenant.id, policy=updated)
    db_session.expire_all()
    assert await load_access_policy(db_session, tenant_id=tenant.id) == updated
