"""Boot provisioning of the configured Teams tenant."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.teams.provisioning import provision_configured_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.accounts import get_account, set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.tenants import get_tenant_liveness, set_provision_status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import ENTRA_TENANT_ID

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)


async def _provision(
    db_factory: async_sessionmaker[AsyncSession], *, failed: bool, admins: tuple[str, ...] = ()
) -> Any:
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
            admin_user_ids=admins,
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


async def test_boot_demotes_an_admin_removed_from_the_list(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Routines and MCP clients read the stored role, so a removed admin loses it at boot."""
    await _provision(db_session_factory, failed=False)
    accounts = {}
    async with db_session_factory.begin() as session:
        for user in ("kept-admin", "removed-admin"):
            principal = await get_or_create_platform_principal(
                session, tenant_id=TENANT, platform="teams", external_id=user
            )
            await set_role(session, principal.account_id, Role.ADMIN)
            accounts[user] = principal.account_id

    await _provision(db_session_factory, failed=False, admins=("kept-admin",))

    async with db_session_factory() as session:
        kept = await get_account(session, accounts["kept-admin"])
        removed = await get_account(session, accounts["removed-admin"])
    assert kept is not None and kept.role is Role.ADMIN, "a listed admin keeps the role"
    assert removed is not None and removed.role is Role.USER, "an unlisted admin is demoted"
