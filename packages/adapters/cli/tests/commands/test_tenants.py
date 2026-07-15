from __future__ import annotations

import json
from io import StringIO
from typing import cast

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.cli.commands.tenants import tenants_delete, tenants_list
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core._models import TenantConfig
from daimon.core.config import Settings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.errors import StoreError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_tenant
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.no_cli_local_seed


class _FakeCli:
    local_user = "testuser"


class _FakeSettings:
    cli = _FakeCli()


def _build_rt(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> CliRuntime:
    return CliRuntime(
        settings=cast(Settings, _FakeSettings()),
        anthropic=stub_anthropic,
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


def _make_console() -> Console:
    """Console that writes to a StringIO for test output capture."""
    return Console(file=StringIO(), force_terminal=False, highlight=False, width=120)


@pytest.mark.asyncio
async def test_tenants_delete_removes_tenant_when_no_dependents(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = _build_rt(db_session_factory, stub_anthropic)
    console = _make_console()

    await provision_tenant(db_session_factory, platform="discord", workspace_id="guild-123")

    await tenants_delete(
        rt=rt,
        console=console,
        platform="discord",
        external_id="guild-123",
        cascade=False,
        yes=True,
    )

    tenant_id = derive_tenant_uuid(platform="discord", workspace_id="guild-123")
    async with db_session_factory() as s, s.begin():
        row = await get_tenant(s, tenant_id)
    assert row is None, "tenant should be deleted when no dependents exist"


@pytest.mark.asyncio
async def test_tenants_delete_refuses_when_dependents_exist_without_cascade(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = _build_rt(db_session_factory, stub_anthropic)
    console = _make_console()

    result = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-456"
    )
    tenant_id = result.tenant_id

    async with db_session_factory() as s, s.begin():
        s.add(TenantConfig(tenant_id=tenant_id, agent_name="a1"))

    with pytest.raises(StoreError, match="dependents"):
        await tenants_delete(
            rt=rt,
            console=console,
            platform="discord",
            external_id="guild-456",
            cascade=False,
            yes=True,
        )

    async with db_session_factory() as s, s.begin():
        row = await get_tenant(s, tenant_id)
    assert row is not None, "tenant should still exist after refused delete"


@pytest.mark.asyncio
async def test_tenants_delete_cascade_deletes_tenant_when_dependents_exist(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = _build_rt(db_session_factory, stub_anthropic)
    console = _make_console()

    result = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-789"
    )
    tenant_id = result.tenant_id

    async with db_session_factory() as s, s.begin():
        s.add(TenantConfig(tenant_id=tenant_id, agent_name="a1"))

    await tenants_delete(
        rt=rt,
        console=console,
        platform="discord",
        external_id="guild-789",
        cascade=True,
        yes=True,
    )

    async with db_session_factory() as s, s.begin():
        row = await get_tenant(s, tenant_id)
    assert row is None, "tenant should be deleted (DB cascade removes dependents)"


@pytest.mark.asyncio
async def test_tenants_delete_raises_when_tenant_not_found(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = _build_rt(db_session_factory, stub_anthropic)
    console = _make_console()

    with pytest.raises(StoreError, match="not found"):
        await tenants_delete(
            rt=rt,
            console=console,
            platform="discord",
            external_id="nonexistent-guild",
            cascade=False,
            yes=True,
        )


@pytest.mark.asyncio
async def test_tenants_list_returns_all_tenants(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = _build_rt(db_session_factory, stub_anthropic)
    console = _make_console()

    async with db_session_factory() as s, s.begin():
        await make_tenant(s, platform="discord", workspace_id="list-guild-1")
        await make_tenant(s, platform="cli", workspace_id="local")

    await tenants_list(rt=rt, console=console, platform=None, as_json=True)

    out = cast(StringIO, console.file).getvalue()
    data = json.loads(out)
    assert len(data) >= 2, "should return at least 2 tenants"
    external_ids = {d["external_id"] for d in data}
    assert "list-guild-1" in external_ids, "should contain discord tenant"
    assert "local" in external_ids, "should contain cli tenant"
    # Verify 5-column shape
    for row in data:
        assert "platform" in row, "row should have platform column"
        assert "external_id" in row, "row should have external_id column"
        assert "provision_status" in row, "row should have provision_status column"
        assert "registered_at" in row, "row should have registered_at column"
        assert "archived_at" in row, "row should have archived_at column"


@pytest.mark.asyncio
async def test_tenants_list_filters_by_platform(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = _build_rt(db_session_factory, stub_anthropic)
    console = _make_console()

    async with db_session_factory() as s, s.begin():
        await make_tenant(s, platform="discord", workspace_id="filter-guild-1")
        await make_tenant(s, platform="cli", workspace_id="filter-local")

    await tenants_list(rt=rt, console=console, platform="discord", as_json=True)

    out = cast(StringIO, console.file).getvalue()
    data = json.loads(out)
    platforms = {d["platform"] for d in data}
    assert platforms == {"discord"}, "should only return discord tenants when filtered"
    cli_ids = [d["external_id"] for d in data if d["platform"] == "cli"]
    assert len(cli_ids) == 0, "cli tenants should be excluded when filtered to discord"
