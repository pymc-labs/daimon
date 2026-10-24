"""Gemini native catalog proofs that need the Daimon host: Postgres-backed catalog and resource host."""

from __future__ import annotations

import os
from typing import cast

import pytest
import pytest_asyncio
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.skills.ingest import SkillIngestError
from daimon.core.stores.neutral_catalog import CatalogConflict, PostgresNeutralCatalog
from daimon.testing.db import (
    build_test_engine,
    require_owned_sessionmaker,
)
from daimon.testing.db import (
    db_engine as db_engine,
)
from daimon.testing.db import (
    db_schema as db_schema,
)
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.ids import Scope
from mux.drivers.gemini import GeminiCatalogAdapter
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from mux.testing.drivers.gemini.catalog_wire import (
    WORKSPACE,
    Case,
    bundle,
    spec,
)
from mux.testing.drivers.gemini.catalog_wire import (
    case as case,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.fixture
def catalog_engine(request: pytest.FixtureRequest) -> AsyncEngine:
    # The mux CI job is deliberately database-free. Opt in only through the
    # normal test DSN and refuse foreign binds before the engine fixture connects.
    dsn = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not dsn:
        pytest.skip("native catalog proofs require an explicit test database")
    schema = cast(str, request.getfixturevalue("db_schema"))
    guard_engine = build_test_engine(dsn, schema)
    try:
        require_owned_sessionmaker(guard_engine)
    except ValueError:
        pytest.skip("native catalog proofs require an owned test database")
    finally:
        # No connection was opened; disposing this empty pool is synchronous.
        guard_engine.sync_engine.dispose()
    return cast(AsyncEngine, request.getfixturevalue("db_engine"))


@pytest_asyncio.fixture
async def catalog_db(
    catalog_engine: AsyncEngine,
) -> tuple[async_sessionmaker[AsyncSession], Scope, Scope]:
    sessions = async_sessionmaker(catalog_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    async with sessions() as session, session.begin():
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        other = await make_account(session, tenant=tenant)
        scopes = tuple(
            Scope(
                tenant_id=str(tenant.id),
                account_id=str(owner.id),
                principal_id="authorized-operator",
                authorization_id="a1-native-catalog",
            )
            for owner in (account, other)
        )
    return sessions, scopes[0], scopes[1]


async def test_native_catalog_cas_retains_old_agent_pin_and_independent_readback(
    case: Case, catalog_db: tuple[async_sessionmaker[AsyncSession], Scope, Scope]
) -> None:
    sessions, scope, foreign = catalog_db
    catalog = PostgresNeutralCatalog(sessions, provider="gemini", account_scope_id=WORKSPACE)
    skill = await case.driver.skills.create(scope, bundle(), key="cas:skill")
    assert skill.latest_version is not None
    original = skill.latest_version
    first = await case.driver.agents.create(scope, spec(pins=(original,)), key="cas:first")
    row = await catalog.put_agent(scope, "logical", first, expected_revision=0)
    next_pin = await case.driver.skills.publish_version(
        scope, skill.id, bundle(b"# New\n"), key="cas:skill:v2"
    )
    second = await case.driver.agents.create(
        scope, spec(pins=(next_pin.ref,)), key="cas:replacement"
    )
    updated = await catalog.put_agent(scope, "logical", second, expected_revision=1)
    restarted = PostgresNeutralCatalog(sessions, provider="gemini", account_scope_id=WORKSPACE)
    assert await restarted.get_agent(scope, "logical") == updated
    assert await restarted.get_agent(scope, "logical", local_revision=1) == row
    assert updated.local_revision == 2
    assert first.ref.id != second.ref.id
    assert first.revision.native is second.revision.native is None
    assert row.agent.spec.skills == (original,)
    assert case.driver.catalog_adapter is not None
    assert await case.driver.catalog_adapter.verify(scope, row.agent) == first
    with pytest.raises(CatalogConflict):
        await restarted.put_agent(scope, "logical", first, expected_revision=1)
    with pytest.raises(ScopeViolation):
        await restarted.put_agent(foreign, "logical", first, expected_revision=0)
    assert await restarted.get_agent(scope, "logical") == updated
    assert await restarted.get_agent(foreign, "logical") is None
    authored_drift = first.model_copy(update={"spec": spec(name="tampered")})
    with pytest.raises(ProviderError, match="catalog_authored_drift"):
        await case.driver.catalog_adapter.verify(scope, authored_drift)
    case.wire.get_changes = {"tools": []}
    with pytest.raises(ProviderError, match="catalog_native_definition_drift"):
        await case.driver.catalog_adapter.verify(scope, updated.agent)


async def test_host_guard_still_refuses_gemini_before_io(
    case: Case, catalog_db: tuple[async_sessionmaker[AsyncSession], Scope, Scope]
) -> None:
    sessions, scope, _ = catalog_db
    with pytest.raises(UnsupportedCapability, match="neutral_resource_host_after_g2"):
        NeutralResourceHost(
            case.driver,
            sessions,
            scope=scope,
            account_scope_id=WORKSPACE,
            operation_key="host:guard",
        )
    assert not case.wire.requests


@pytest.mark.parametrize(
    "edge", ["missing", "disabled", "provider", "profile", "workspace", "port", "truthy"]
)
async def test_host_requires_the_explicit_matching_native_adapter_before_io(
    case: Case,
    catalog_db: tuple[async_sessionmaker[AsyncSession], Scope, Scope],
    monkeypatch: pytest.MonkeyPatch,
    edge: str,
) -> None:
    sessions, scope, _ = catalog_db
    adapter = case.driver.catalog_adapter
    assert adapter is not None
    workspace = WORKSPACE
    flag = True
    supplied: GeminiCatalogAdapter | None = adapter
    if edge == "missing":
        supplied = None
    elif edge == "disabled":
        monkeypatch.setattr(adapter, "enabled", False)
    elif edge == "provider":
        monkeypatch.setattr(adapter, "provider", "openai")
    elif edge == "profile":
        monkeypatch.setattr(adapter, "profile_id", "gemini.other")
    elif edge == "workspace":
        workspace = "foreign-workspace"
    elif edge == "truthy":
        flag = cast(bool, 1)
    else:
        supplied = GeminiCatalogAdapter(
            case.interactions, case.storage, WORKSPACE, case.native, enabled=True
        )
    with pytest.raises(UnsupportedCapability, match="neutral_resource_host_after_g2"):
        NeutralResourceHost(
            case.driver,
            sessions,
            scope=scope,
            account_scope_id=workspace,
            operation_key="host:negative",
            resource_catalog_enabled=flag,
            catalog_adapter=supplied,
        )
    assert not case.wire.requests


async def test_enabled_host_publishes_and_reads_native_then_refuses_scope_or_drift(
    case: Case, catalog_db: tuple[async_sessionmaker[AsyncSession], Scope, Scope]
) -> None:
    sessions, scope, foreign = catalog_db
    host = NeutralResourceHost(
        case.driver,
        sessions,
        scope=scope,
        account_scope_id=WORKSPACE,
        operation_key="host:positive",
        resource_catalog_enabled=True,
        catalog_adapter=case.driver.catalog_adapter,
    )
    row = await host.publish("source", spec(), expected_revision=0, key="host:source")
    assert await host.read("source") == row
    assert [r.method for r in case.wire.requests] == ["POST", "GET", "GET"]
    before = len(case.wire.requests)
    foreign_host = NeutralResourceHost(
        case.driver,
        sessions,
        scope=foreign,
        account_scope_id=WORKSPACE,
        operation_key="host:foreign",
        resource_catalog_enabled=True,
        catalog_adapter=case.driver.catalog_adapter,
    )
    with pytest.raises(SkillIngestError):
        await foreign_host.read("source")
    assert len(case.wire.requests) == before
    with pytest.raises(CatalogConflict):
        await host.publish("source", spec(name="stale"), expected_revision=0, key="stale")
    assert len(case.wire.requests) == before
    with pytest.raises(ScopeViolation):
        NeutralResourceHost(
            case.driver,
            sessions,
            scope=scope.model_copy(update={"authorization_id": " "}),
            account_scope_id=WORKSPACE,
            operation_key="host:unbound",
            resource_catalog_enabled=True,
            catalog_adapter=case.driver.catalog_adapter,
        )
    assert len(case.wire.requests) == before
    case.wire.get_changes = {"tools": []}
    with pytest.raises(ProviderError, match="catalog_native_definition_drift"):
        await host.read("source")
