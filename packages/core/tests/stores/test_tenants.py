"""Tests for daimon.core.stores.tenants store helpers."""

from __future__ import annotations

import uuid

import pytest
from daimon.core._models import TenantConfig
from daimon.core.errors import StoreError
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.tenants import (
    delete_tenant,
    get_tenant,
    get_tenant_dependent_counts,
    get_tenant_liveness,
    set_provision_status,
)
from daimon.testing.factories import make_tenant
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


async def test_get_tenant_returns_tenant_row_with_all_fields(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, workspace_id="guild-get-fields")

    row = await get_tenant(db_session, tenant.id)

    assert isinstance(row, TenantRow), "store should map ORM -> Pydantic TenantRow"
    assert row.id == tenant.id, "TenantRow.id should match ORM tenant.id"
    assert row.platform == "discord", "TenantRow.platform should match what was inserted"
    assert row.external_id == "guild-get-fields", "TenantRow.external_id should match workspace_id"
    assert row.provision_status == "ready", "default provision_status should be 'ready'"
    assert row.archived_at is None, "freshly created tenant should not be archived"
    assert row.registered_at is not None, "registered_at should be set by server default"
    assert row.created_at is not None, "created_at should be set by server default"


async def test_get_tenant_returns_none_for_unknown_id(db_session: AsyncSession) -> None:
    result = await get_tenant(db_session, uuid.uuid4())
    assert result is None, "unknown tenant_id should return None, not raise"


async def test_get_tenant_liveness_returns_tenant_row_when_exists(
    db_session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """get_tenant_liveness opens its own session and returns TenantRow for an existing tenant."""
    tenant = await make_tenant(db_session, workspace_id="guild-liveness-found")
    await db_session.flush()

    row = await get_tenant_liveness(db_session_factory, tenant.id)

    assert row is not None, "existing tenant must be found via get_tenant_liveness"
    assert isinstance(row, TenantRow), "get_tenant_liveness must return a TenantRow (Pydantic)"
    assert row.id == tenant.id, "TenantRow.id must match the seeded tenant"
    assert row.external_id == "guild-liveness-found", "TenantRow.external_id must round-trip"


async def test_get_tenant_liveness_returns_none_for_unknown_id(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """get_tenant_liveness returns None (not raise) for an unknown tenant_id."""
    result = await get_tenant_liveness(db_session_factory, uuid.uuid4())
    assert result is None, "unknown tenant_id must return None, not raise"


async def test_delete_tenant_removes_row_and_cascades(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """delete_tenant removes the tenant row; ON DELETE CASCADE wipes dependents."""
    tenant = await make_tenant(db_session, workspace_id="guild-delete-cascade")
    # Seed a dependent TenantConfig row
    tc = TenantConfig(tenant_id=tenant.id, agent_name="cascade-test")
    db_session.add(tc)
    await db_session.flush()

    await delete_tenant(db_session, tenant_id=tenant.id)
    await db_session.flush()

    assert await get_tenant(db_session, tenant.id) is None, "tenant row must be gone after delete"
    # TenantConfig cascades from tenants FK ON DELETE CASCADE
    remaining_tc = (
        (await db_session.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant.id)))
        .scalars()
        .all()
    )
    assert len(remaining_tc) == 0, "TenantConfig rows must be cascade-deleted with the tenant"


async def test_delete_tenant_raises_when_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(StoreError, match="not found"):
        await delete_tenant(db_session, tenant_id=uuid.uuid4())


async def test_get_tenant_dependent_counts_returns_per_table_breakdown(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, workspace_id="guild-counts")
    # Seed two distinct tables to verify per-table fields
    db_session.add(TenantConfig(tenant_id=tenant.id, agent_name="a"))
    await db_session.flush()

    counts = await get_tenant_dependent_counts(db_session, tenant_id=tenant.id)

    assert counts.tenant_config == 1, "should count the seeded TenantConfig row"
    assert counts.routines == 0, "no routines seeded"
    assert counts.usage_events == 0, "no usage_events seeded"
    assert counts.total == 1, "total should equal sum of per-table fields"


async def test_get_tenant_dependent_counts_returns_zeros_when_no_dependents(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, workspace_id="guild-counts-zero")

    counts = await get_tenant_dependent_counts(db_session, tenant_id=tenant.id)

    assert counts.total == 0, "total must be zero when no dependent rows exist"
    assert counts.routines == 0, "routines must be zero"
    assert counts.tenant_config == 0, "tenant_config must be zero"


async def test_set_provision_status_updates_tenant(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """set_provision_status writes provision_status and archived_at on the tenant row."""
    await make_tenant(db_session, workspace_id="guild-status")
    # Need to get the tenant_id from what we just inserted
    from daimon.core.ma_identity import derive_tenant_uuid  # noqa: PLC0415

    tenant_id = derive_tenant_uuid(platform="discord", workspace_id="guild-status")

    # Set status to "failed"
    await set_provision_status(db_session_factory, tenant_id=tenant_id, status="failed")
    row = await get_tenant(db_session, tenant_id)
    assert row is not None, "tenant must exist after set_provision_status"
    assert row.provision_status == "failed", "provision_status should be updated to 'failed'"

    # Archive the tenant
    await set_provision_status(db_session_factory, tenant_id=tenant_id, archive=True)
    archived_row = await get_tenant(db_session, tenant_id)
    assert archived_row is not None
    assert archived_row.archived_at is not None, "archive=True must set archived_at"
