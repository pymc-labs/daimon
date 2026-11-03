"""Tests for daimon.core.stores.tenant_user_caps, the legacy cap table kept for erasure."""

from __future__ import annotations

from decimal import Decimal

from daimon.core._models import TenantUserCap
from daimon.core.stores import tenant_user_caps
from daimon.testing.factories import make_tenant
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def test_delete_all_for_user_removes_overrides_only_not_default(
    db_session: AsyncSession,
) -> None:
    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)
    db_session.add_all(
        [
            TenantUserCap(tenant_id=tenant_a.id, platform_user_id=None, cap_usd=Decimal("5.00")),
            TenantUserCap(tenant_id=tenant_a.id, platform_user_id="u1", cap_usd=Decimal("50.00")),
            TenantUserCap(tenant_id=tenant_b.id, platform_user_id="u1", cap_usd=Decimal("100.00")),
        ]
    )
    await db_session.flush()

    deleted = await tenant_user_caps.delete_all_for_user(
        db_session, tenant_id=tenant_a.id, platform_user_id="u1"
    )

    assert deleted == 1, (
        "delete must be tenant-scoped: only tenant_a's u1 override, not tenant_b's "
        "(Slack user ids collide across workspaces)"
    )
    rows_a = (
        (
            await db_session.execute(
                select(TenantUserCap).where(TenantUserCap.tenant_id == tenant_a.id)
            )
        )
        .scalars()
        .all()
    )
    assert [r.platform_user_id for r in rows_a] == [None], (
        "the surviving row must be the NULL-user_id default, never the tenant's default deleted"
    )
    rows_b = (
        (
            await db_session.execute(
                select(TenantUserCap).where(TenantUserCap.tenant_id == tenant_b.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows_b) == 1, "another tenant's identically-named user override must survive"
