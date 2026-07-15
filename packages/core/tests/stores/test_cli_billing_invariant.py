"""Regression guard: cli:local provisioning must write zero tenant_ledger rows (REQ-7b).

get_or_create_cli_principal creates Account + CliPrincipal only. It never
imports tenant_ledger or calls provision_guild. This test proves the structural
guarantee by asserting zero tenant_ledger rows after provisioning — catching
any future regression where ledger seeding is accidentally introduced on the
CLI path.
"""

from __future__ import annotations

import pytest
from daimon.core._models import TenantLedger
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.testing.factories import make_tenant
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def test_cli_local_provisioning_writes_no_tenant_ledger(
    db_session: AsyncSession,
) -> None:
    """cli:local provisioning must write zero tenant_ledger rows.

    Calls get_or_create_cli_principal (the cli:local identity path) and asserts
    SELECT count(*) FROM tenant_ledger == 0. This proves the CLI path never
    seeds a trial ledger entry, even when a tenant and account are created.
    """
    tenant = await make_tenant(db_session)

    await get_or_create_cli_principal(db_session, tenant_id=tenant.id, os_user="testuser")

    count = await db_session.scalar(select(func.count()).select_from(TenantLedger))
    assert count == 0, "cli:local provisioning must write zero tenant_ledger rows"
