from __future__ import annotations

import uuid

import pytest
from daimon.adapters.cli.sessions_bootstrap import SessionBootstrapError
from daimon.adapters.cli.tenant import discover_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.no_cli_local_seed


def test_probe_cli_tenant_uuid_differs_from_local() -> None:
    assert derive_tenant_uuid(platform="cli", workspace_id="local") != derive_tenant_uuid(
        platform="cli", workspace_id="agent-setup-probe"
    )


@pytest.mark.asyncio
async def test_discover_tenant_returns_cli_local_when_present(db_session: AsyncSession) -> None:
    cli_local = await make_tenant(db_session, platform="cli", workspace_id="local")
    await make_tenant(db_session, platform="discord", workspace_id="some-guild")
    tenant_id = await discover_tenant(db_session)
    assert tenant_id == cli_local.id, "should return the derived cli:local tenant, not any other"


@pytest.mark.asyncio
async def test_discover_tenant_uses_selected_cli_workspace(db_session: AsyncSession) -> None:
    await make_tenant(db_session, platform="cli", workspace_id="local")
    selected = await make_tenant(db_session, platform="cli", workspace_id="agent-setup-probe")
    tenant_id = await discover_tenant(db_session, workspace_id="agent-setup-probe")
    assert tenant_id == selected.id, "selected CLI workspace must stay separate from cli:local"


@pytest.mark.asyncio
async def test_discover_tenant_raises_when_no_tenants(db_session: AsyncSession) -> None:
    with pytest.raises(SessionBootstrapError, match="no tenant"):
        await discover_tenant(db_session)


@pytest.mark.asyncio
async def test_discover_tenant_override_returns_valid(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    tenant_id = await discover_tenant(db_session, override=t.id)
    assert tenant_id == t.id, "override should return the specified tenant"


@pytest.mark.asyncio
async def test_discover_tenant_override_raises_on_invalid(db_session: AsyncSession) -> None:
    bogus = uuid.uuid4()
    with pytest.raises(SessionBootstrapError, match="not found"):
        await discover_tenant(db_session, override=bogus)
