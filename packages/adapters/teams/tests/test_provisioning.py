"""Boot provisioning of the configured Teams tenant."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.teams.provisioning import provision_configured_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.tenants import get_tenant_liveness, set_provision_status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import ENTRA_TENANT_ID

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)


async def _provision(db_factory: async_sessionmaker[AsyncSession], *, failed: bool) -> Any:
    report = MagicMock()
    report.is_failure.return_value = failed
    with (
        patch(
            "daimon.adapters.teams.provisioning.reconcile_tenant_defaults",
            AsyncMock(return_value=report),
        ),
        patch(
            "daimon.adapters.teams.provisioning.compose_failure_reason",
            return_value="agent 'daimon': boom",
        ),
        patch(
            "daimon.adapters.teams.provisioning.find_agent_by_daimon_tag",
            AsyncMock(return_value=object()),
        ),
    ):
        await provision_configured_tenant(
            anthropic=AsyncMock(),
            sessionmaker=db_factory,
            defaults_root=Path("defaults"),
            deployment_default=DeploymentDefault(agent_name="daimon"),
            public_url=None,
            entra_tenant_id=ENTRA_TENANT_ID,
            signup_credit=Decimal("0"),
        )
    return await get_tenant_liveness(db_factory, TENANT)


async def test_first_boot_provisions_a_ready_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _provision(db_session_factory, failed=False)
    assert tenant is not None and tenant.platform == "teams"
    assert tenant.external_id == ENTRA_TENANT_ID and tenant.provision_status == "ready"


async def test_a_failed_first_reconcile_leaves_the_tenant_failed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _provision(db_session_factory, failed=True)
    assert tenant is not None and tenant.provision_status == "failed"
    assert tenant.last_reconcile_error == "agent 'daimon': boom"


async def test_a_failed_reconcile_keeps_a_ready_tenant_ready(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _provision(db_session_factory, failed=False)
    tenant = await _provision(db_session_factory, failed=True)
    assert tenant is not None and tenant.provision_status == "ready"
    assert tenant.last_reconcile_error == "agent 'daimon': boom"


async def test_an_archived_tenant_is_left_alone(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _provision(db_session_factory, failed=False)
    await set_provision_status(db_session_factory, tenant_id=TENANT, archive=True)
    tenant = await _provision(db_session_factory, failed=True)
    assert tenant is not None and tenant.archived_at is not None
    assert tenant.last_reconcile_error is None
