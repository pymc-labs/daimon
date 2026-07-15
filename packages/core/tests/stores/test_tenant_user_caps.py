"""Tests for daimon.core.stores.tenant_user_caps — BILL-01."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from daimon.core._models import TenantUserCap
from daimon.core.stores import tenant_user_caps
from daimon.testing.factories import make_tenant
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def tenant_id(db_session: AsyncSession) -> uuid.UUID:
    tenant = await make_tenant(db_session)
    return tenant.id


async def test_set_default_inserts_row_with_null_user_id(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    row = await tenant_user_caps.set_default(
        db_session,
        tenant_id=tenant_id,
        amount=Decimal("10.00"),
    )
    assert row.platform_user_id is None, "default row must have NULL platform_user_id"
    assert row.cap_usd == Decimal("10.00"), "cap_usd must match the supplied amount"


async def test_set_default_twice_replaces_not_duplicates(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    """NULLS NOT DISTINCT — the second set_default upserts the same row."""
    await tenant_user_caps.set_default(
        db_session,
        tenant_id=tenant_id,
        amount=Decimal("10.00"),
    )
    await tenant_user_caps.set_default(
        db_session,
        tenant_id=tenant_id,
        amount=Decimal("20.00"),
    )
    cap = await tenant_user_caps.get_effective_cap(
        db_session,
        tenant_id=tenant_id,
        user_id="anyone",
    )
    assert cap == Decimal("20.00"), "second set_default should overwrite the first"

    result = await db_session.execute(
        select(func.count())
        .select_from(TenantUserCap)
        .where(
            TenantUserCap.tenant_id == tenant_id,
        )
    )
    assert result.scalar_one() == 1, "NULLS NOT DISTINCT means only one default row per tenant"


async def test_set_override_inserts_row_with_user_id_distinct_from_default(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    await tenant_user_caps.set_default(
        db_session,
        tenant_id=tenant_id,
        amount=Decimal("5.00"),
    )
    await tenant_user_caps.set_override(
        db_session,
        tenant_id=tenant_id,
        user_id="u1",
        amount=Decimal("50.00"),
    )
    result = await db_session.execute(
        select(func.count())
        .select_from(TenantUserCap)
        .where(
            TenantUserCap.tenant_id == tenant_id,
        )
    )
    assert result.scalar_one() == 2, "default and override coexist as two distinct rows"


async def test_get_effective_cap_override_beats_default(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    await tenant_user_caps.set_default(
        db_session,
        tenant_id=tenant_id,
        amount=Decimal("5.00"),
    )
    await tenant_user_caps.set_override(
        db_session,
        tenant_id=tenant_id,
        user_id="u1",
        amount=Decimal("50.00"),
    )
    cap = await tenant_user_caps.get_effective_cap(
        db_session,
        tenant_id=tenant_id,
        user_id="u1",
    )
    assert cap == Decimal("50.00"), "override should beat default"


async def test_get_effective_cap_default_when_no_override(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    await tenant_user_caps.set_default(
        db_session,
        tenant_id=tenant_id,
        amount=Decimal("5.00"),
    )
    cap = await tenant_user_caps.get_effective_cap(
        db_session,
        tenant_id=tenant_id,
        user_id="u_nobody",
    )
    assert cap == Decimal("5.00"), "users with no override should fall back to default"


async def test_get_effective_cap_returns_none_when_no_row(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    cap = await tenant_user_caps.get_effective_cap(
        db_session,
        tenant_id=tenant_id,
        user_id="u1",
    )
    assert cap is None, "no rows means no cap enforced"


async def test_clear_override_removes_user_row_keeps_default(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    await tenant_user_caps.set_default(
        db_session,
        tenant_id=tenant_id,
        amount=Decimal("5.00"),
    )
    await tenant_user_caps.set_override(
        db_session,
        tenant_id=tenant_id,
        user_id="u1",
        amount=Decimal("50.00"),
    )
    await tenant_user_caps.clear_override(
        db_session,
        tenant_id=tenant_id,
        user_id="u1",
    )
    cap = await tenant_user_caps.get_effective_cap(
        db_session,
        tenant_id=tenant_id,
        user_id="u1",
    )
    assert cap == Decimal("5.00"), (
        "after clearing override, user falls back to the still-present default"
    )


async def test_delete_all_for_user_removes_overrides_only_not_default(
    db_session: AsyncSession,
) -> None:
    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)

    await tenant_user_caps.set_default(
        db_session,
        tenant_id=tenant_a.id,
        amount=Decimal("5.00"),
    )
    await tenant_user_caps.set_override(
        db_session,
        tenant_id=tenant_a.id,
        user_id="u1",
        amount=Decimal("50.00"),
    )
    await tenant_user_caps.set_override(
        db_session,
        tenant_id=tenant_b.id,
        user_id="u1",
        amount=Decimal("100.00"),
    )
    deleted = await tenant_user_caps.delete_all_for_user(
        db_session,
        tenant_id=tenant_a.id,
        platform_user_id="u1",
    )
    assert deleted == 1, (
        "delete must be tenant-scoped: only tenant_a's u1 override, not tenant_b's "
        "(Slack user ids collide across workspaces)"
    )

    # tenant_a keeps its default; its u1 override is gone.
    result_a = await db_session.execute(
        select(TenantUserCap).where(TenantUserCap.tenant_id == tenant_a.id)
    )
    rows_a = result_a.scalars().all()
    assert len(rows_a) == 1, "only tenant_a's override row should be deleted"
    assert rows_a[0].platform_user_id is None, (
        "the surviving row must be the NULL-user_id default — never delete the tenant's default"
    )

    # tenant_b's identically-named u1 override must be untouched.
    result_b = await db_session.execute(
        select(TenantUserCap).where(
            TenantUserCap.tenant_id == tenant_b.id,
            TenantUserCap.platform_user_id == "u1",
        )
    )
    assert len(result_b.scalars().all()) == 1, (
        "another tenant's identically-named user override must survive the purge"
    )


# ---------------------------------------------------------------------------
# Cross-tenant isolation test (R-8)
# ---------------------------------------------------------------------------


async def test_tenant_user_caps_isolation(db_session: AsyncSession) -> None:
    # Seed two tenants inline (no shared state between tests)
    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)

    # Write under tenant_a (set default cap)
    await tenant_user_caps.set_default(db_session, tenant_id=tenant_a.id, amount=Decimal("10.00"))

    # Read under tenant_a — sees own cap
    cap_a = await tenant_user_caps.get_effective_cap(
        db_session, tenant_id=tenant_a.id, user_id="any_user"
    )
    assert cap_a is not None, "tenant_a should see its own default cap"

    # Read under tenant_b — sees nothing (no cap set)
    cap_b = await tenant_user_caps.get_effective_cap(
        db_session, tenant_id=tenant_b.id, user_id="any_user"
    )
    assert cap_b is None, "tenant_b must not see tenant_a's cap"

    # Write under tenant_b, re-read tenant_a — cap must be unchanged
    await tenant_user_caps.set_default(db_session, tenant_id=tenant_b.id, amount=Decimal("5.00"))
    cap_a_after = await tenant_user_caps.get_effective_cap(
        db_session, tenant_id=tenant_a.id, user_id="any_user"
    )
    assert cap_a_after == cap_a, "tenant_b write must not affect tenant_a cap reads"
