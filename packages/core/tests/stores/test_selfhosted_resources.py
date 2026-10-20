"""Self-hosted ports preserve tenant boundaries and durable mutation receipts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from daimon.core.stores.selfhosted_resources import (
    SelfHostedAgents,
    SelfHostedEnvironments,
)
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, PageRequest, Revision, Scope
from mux.contracts.ports import Agents, Environments
from mux.contracts.resources import (
    AgentFilter,
    AgentPatch,
    AgentSpec,
    EnvironmentFilter,
    EnvironmentPatch,
    EnvironmentSpec,
    MCPConnection,
    SkillRef,
)
from mux.drivers.selfhosted.config import HarnessConfig
from mux.errors import OperationConflict, ProviderError, ScopeViolation
from mux.profiles.selfhosted import LOOP
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def scope(tenant: str, account: str | None = None) -> Scope:
    return Scope(
        tenant_id=tenant,
        account_id=account or str(uuid4()),
        principal_id="operator",
        authorization_id="test",
    )


def agent_spec(name: str = "daimon") -> AgentSpec:
    return AgentSpec(
        name=name,
        model=ModelRef(provider=LOOP.provider, id="gpt-5"),
        system="Be helpful.",
        mcp_servers=(
            MCPConnection(
                name="tools",
                url="https://tools.example/mcp",
                credential_ref="HARNESS_MCP_CRED_TOOLS",
            ),
        ),
        skills=(SkillRef(id="analysis"),),
        metadata={"purpose": "test"},
    )


def environment_spec(name: str = "default") -> EnvironmentSpec:
    return EnvironmentSpec(
        name=name,
        execution="self_hosted",
        native_config=ExtensionConfig(
            namespace="selfhosted.environment",
            version=1,
            value={
                "harness_url": "https://harness.example",
                "auth_mode": "bearer_env",
                "auth_ref": "HARNESS_TOKEN",
                "packages_note": "pymc",
            },
        ),
    )


async def test_ports_roundtrip_idempotency_and_tenant_isolation(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    other = await make_tenant(db_session)
    await db_session.commit()
    authorized = scope(str(tenant.id), str(account.id))
    agents: Agents = SelfHostedAgents(db_session_factory)
    environments: Environments = SelfHostedEnvironments(db_session_factory)
    agent = await agents.create(authorized, agent_spec(), key="agent")
    env = await environments.create(authorized, environment_spec(), key="env")
    assert agent.ref.id.startswith("sh_agent_")
    assert env.ref.id.startswith("sh_env_")
    assert agent.spec == agent_spec()
    assert env.spec == environment_spec()
    assert await agents.retrieve(authorized, agent.ref) == agent
    assert await environments.retrieve(authorized, env.ref) == env
    assert (
        await SelfHostedAgents(db_session_factory).create(authorized, agent_spec(), key="agent")
        == agent
    )
    with pytest.raises(OperationConflict):
        await agents.create(authorized, agent_spec("changed"), key="agent")
    for ref, port in ((agent.ref, agents), (env.ref, environments)):
        with pytest.raises(ScopeViolation):
            await port.retrieve(scope(str(other.id)), ref)
        forged = ref.model_copy(
            update={"tenant_id": str(other.id), "account_scope_id": str(other.id)}
        )
        with pytest.raises(ProviderError) as error:
            await port.retrieve(scope(str(other.id)), forged)
        assert error.value.category == "not_found"
    assert not (
        await agents.list(scope(str(other.id)), filters=AgentFilter(), page=PageRequest())
    ).data


async def test_update_archive_delete_and_replay(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    authorized = scope(str(tenant.id), str(account.id))
    agents = SelfHostedAgents(db_session_factory)
    agent = await agents.create(authorized, agent_spec(), key="create")
    updated = await agents.update(
        authorized,
        agent.ref,
        AgentPatch(system="New prompt", metadata={"purpose": None, "added": "yes"}),
        expected=agent.revision,
        key="update",
    )
    assert updated.spec.system == "New prompt"
    assert dict(updated.spec.metadata or {}) == {"added": "yes"}
    assert updated.spec.mcp_servers == agent.spec.mcp_servers
    assert updated.revision.local == 2
    assert (
        await agents.update(
            authorized,
            agent.ref,
            AgentPatch(system="New prompt", metadata={"purpose": None, "added": "yes"}),
            expected=agent.revision,
            key="update",
        )
        == updated
    )
    with pytest.raises(ProviderError) as error:
        await agents.update(
            authorized, agent.ref, AgentPatch(name="oops"), expected=Revision(local=1), key="stale"
        )
    assert error.value.category == "conflict"
    archived = await agents.archive(authorized, agent.ref, key="archive")
    assert await agents.archive(authorized, agent.ref, key="archive") == archived
    assert not (await agents.list(authorized, filters=AgentFilter(), page=PageRequest())).data
    assert (await agents.retrieve(authorized, agent.ref)).archived_at is not None
    receipt = await agents.delete(authorized, agent.ref, key="delete")
    assert receipt.deleted == (agent.ref,)
    assert (
        await SelfHostedAgents(db_session_factory).delete(authorized, agent.ref, key="delete")
        == receipt
    )
    with pytest.raises(ProviderError):
        await agents.retrieve(authorized, agent.ref)


async def test_pagination_filters_and_environment_update(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    authorized = scope(str(tenant.id), str(account.id))
    agents = SelfHostedAgents(db_session_factory)
    for name in ("one", "two", "three"):
        await agents.create(authorized, agent_spec(name), key=name)
    first = await agents.list(authorized, filters=AgentFilter(), page=PageRequest(limit=2))
    second = await agents.list(
        authorized, filters=AgentFilter(), page=PageRequest(limit=2, cursor=first.next_cursor)
    )
    assert first.has_more and not second.has_more
    assert len({a.ref.id for a in (*first.data, *second.data)}) == 3
    assert (
        len(
            (
                await agents.list(authorized, filters=AgentFilter(name="two"), page=PageRequest())
            ).data
        )
        == 1
    )
    assert not (
        await agents.list(
            authorized,
            filters=AgentFilter(created_after=datetime(2100, 1, 1, tzinfo=UTC)),
            page=PageRequest(),
        )
    ).data
    envs = SelfHostedEnvironments(db_session_factory)
    env = await envs.create(authorized, environment_spec(), key="env")
    changed = await envs.update(
        authorized, env.ref, EnvironmentPatch(name="renamed"), expected=env.revision, key="rename"
    )
    assert changed.spec.native_config == env.spec.native_config
    assert (await envs.resolve(authorized, "renamed")).ref == env.ref
    assert (
        await envs.list(authorized, filters=EnvironmentFilter(name="renamed"), page=PageRequest())
    ).data == (changed,)


@pytest.mark.parametrize(
    "config",
    [
        {"harness_url": "https://user:secret@example.com", "auth_ref": "TOKEN"},
        {"harness_url": "https://example.com?token=secret", "auth_ref": "TOKEN"},
        {"harness_url": "https://example.com", "auth_ref": "Bearer secret"},
    ],
)
def test_harness_rejects_inline_credentials(config: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        HarnessConfig.model_validate(config)


async def test_concurrent_retries_commit_one_resource(db_committing_engine):
    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    async with sessions.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
    authorized = scope(str(tenant.id), str(account.id))
    agents = SelfHostedAgents(sessions)
    results = await asyncio.gather(
        *(agents.create(authorized, agent_spec(), key="same-operation") for _ in range(4))
    )
    assert len({result.ref.id for result in results}) == 1
    assert len((await agents.list(authorized, filters=AgentFilter(), page=PageRequest())).data) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"agent_name": "daimon"},
        {"project_endpoint": "https://foundry.example/api/projects/test"},
        {"agent_name": "../other", "project_endpoint": "https://foundry.example"},
        {"agent_name": "daimon", "project_endpoint": "https://user:secret@foundry.example"},
    ],
)
def test_foundry_transport_requires_safe_routing(extra):
    with pytest.raises(ValueError):
        HarnessConfig.model_validate(
            {
                "harness_url": "https://harness.example",
                "auth_ref": "api://audience",
                "auth_mode": "entra",
                "transport": "foundry_invocations",
                **extra,
            }
        )


def test_foundry_transport_retains_project_and_agent():
    config = HarnessConfig(
        harness_url="https://harness.example",
        auth_mode="entra",
        auth_ref="api://audience",
        transport="foundry_invocations",
        agent_name="daimon",
        project_endpoint="https://foundry.example/api/projects/test",
    )
    assert HarnessConfig.model_validate(config.model_dump()) == config
    assert config.transport == "foundry_invocations"
    assert (
        HarnessConfig(harness_url="http://localhost:8080", auth_ref="HARNESS_TOKEN").transport
        == "direct"
    )


async def test_receipts_are_reference_only_owned_and_separate_by_account_and_kind(
    db_session, db_session_factory
):
    from daimon.core._models import SelfHostedResourceOperation
    from sqlalchemy import select

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    authorized = scope(str(tenant.id), str(account.id))
    agents = SelfHostedAgents(db_session_factory)
    agent = await agents.create(authorized, agent_spec(), key="same-key")
    env = await SelfHostedEnvironments(db_session_factory).create(
        authorized, environment_spec(), key="same-key"
    )
    with pytest.raises(ScopeViolation):
        await agents.create(
            authorized.model_copy(update={"principal_id": "other-principal"}),
            agent_spec(),
            key="same-key",
        )
    other_row = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    other_account = authorized.model_copy(
        update={"account_id": str(other_row.id), "principal_id": "other-principal"}
    )
    other = await agents.create(other_account, agent_spec("other"), key="same-key")
    assert len({agent.ref.id, env.ref.id, other.ref.id}) == 3
    async with db_session_factory() as db:
        receipts = list(await db.scalars(select(SelfHostedResourceOperation)))
        assert len(receipts) == 3
        assert all(set(receipt.result) == {"ref", "revision"} for receipt in receipts)
        assert all(receipt.expires_at > receipt.created_at for receipt in receipts)
        assert {receipt.principal_id for receipt in receipts} == {"operator", "other-principal"}


async def test_deleted_create_replay_fails_and_archived_names_can_be_reused(
    db_session, db_session_factory
):
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    authorized = scope(str(tenant.id), str(account.id))
    agents = SelfHostedAgents(db_session_factory)
    old = await agents.create(authorized, agent_spec(), key="create")
    await agents.archive(authorized, old.ref, key="archive")
    for name in (old.ref.id, old.spec.name):
        with pytest.raises(ProviderError) as error:
            await agents.resolve(authorized, name)
        assert error.value.category == "not_found"
    new = await agents.create(authorized, agent_spec(), key="replacement")
    assert new.ref.id != old.ref.id
    assert (await agents.resolve(authorized, old.spec.name)).ref == new.ref
    assert (await agents.retrieve(authorized, old.ref)).archived_at is not None
    await agents.delete(authorized, new.ref, key="delete")
    with pytest.raises(ProviderError) as error:
        await agents.create(authorized, agent_spec(), key="replacement")
    assert error.value.category == "not_found"


async def test_explicit_null_patches_clear_optional_fields(db_session, db_session_factory):
    from daimon.core._models import SelfHostedAgentRecord

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    authorized = scope(str(tenant.id), str(account.id))
    agents = SelfHostedAgents(db_session_factory)
    agent = await agents.create(authorized, agent_spec(), key="agent")
    # Simulate a record from before the v1 extension allowlist. Clearing it
    # must remove the stored data rather than preserve an unvalidated payload.
    row = await db_session.get(SelfHostedAgentRecord, agent.ref.id)
    assert row is not None
    row.spec = {
        **row.spec,
        "extensions": [
            {"namespace": "selfhosted.legacy", "version": 1, "value": {"mode": "legacy"}}
        ],
    }
    await db_session.commit()
    cleared = await agents.update(
        authorized,
        agent.ref,
        AgentPatch(metadata=None, extensions=None, system=None, skills=None),
        expected=agent.revision,
        key="clear",
    )
    assert cleared.spec.metadata is None and cleared.spec.extensions is None
    assert cleared.spec.system is None and cleared.spec.skills is None
    assert cleared.spec.mcp_servers == agent.spec.mcp_servers
    envs = SelfHostedEnvironments(db_session_factory)
    env_spec = EnvironmentSpec.model_validate(
        {**environment_spec().model_dump(), "metadata": {"note": "keep"}, "description": "clear"}
    )
    env = await envs.create(authorized, env_spec, key="env")
    env = await envs.update(
        authorized,
        env.ref,
        EnvironmentPatch(metadata=None, description=None),
        expected=env.revision,
        key="clear",
    )
    assert env.spec.metadata is None and env.spec.description is None
    assert env.spec.native_config == env_spec.native_config


async def test_receipt_ttl_prunes_idle_and_active_tenants(db_session, db_session_factory):
    from datetime import timedelta

    from daimon.core._models import SelfHostedResourceOperation
    from daimon.core.stores.selfhosted_resources import cleanup_expired_receipts
    from sqlalchemy import select, update

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    authorized = scope(str(tenant.id), str(account.id))
    agents = SelfHostedAgents(db_session_factory)
    await agents.create(authorized, agent_spec(), key="expired")
    async with db_session_factory.begin() as db:
        await db.execute(
            update(SelfHostedResourceOperation).values(
                expires_at=datetime.now(UTC) - timedelta(days=1)
            )
        )
    assert await cleanup_expired_receipts(db_session_factory) == 1
    await agents.create(authorized, agent_spec("after-expiry"), key="expired")
    async with db_session_factory.begin() as db:
        await db.execute(
            update(SelfHostedResourceOperation).values(
                expires_at=datetime.now(UTC) - timedelta(days=1)
            )
        )
    await agents.create(authorized, agent_spec("prunes-on-write"), key="new")
    async with db_session_factory() as db:
        receipts = list(await db.scalars(select(SelfHostedResourceOperation)))
        assert len(receipts) == 1 and receipts[0].key == "new"


async def test_bad_input_uses_mux_errors_and_does_not_write_receipts(
    db_session, db_session_factory
):
    from daimon.core._models import SelfHostedResourceOperation
    from sqlalchemy import func, select

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    authorized = scope(str(tenant.id), str(account.id))
    agents = SelfHostedAgents(db_session_factory)
    for name in ("", "sh_reserved"):
        with pytest.raises(ProviderError) as error:
            await agents.create(authorized, agent_spec(name), key="invalid")
        assert error.value.category == "invalid_request"
    with pytest.raises(ScopeViolation):
        await agents.create(
            authorized.model_copy(update={"tenant_id": "invalid"}), agent_spec(), key="invalid"
        )
    with pytest.raises(ProviderError) as error:
        await agents.create(
            authorized.model_copy(update={"tenant_id": str(uuid4())}), agent_spec(), key="unknown"
        )
    assert error.value.category == "not_found"
    secret_spec = AgentSpec.model_validate(
        {**agent_spec().model_dump(), "metadata": {"api_key": "opaque-value"}}
    )
    with pytest.raises(ProviderError) as error:
        await agents.create(authorized, secret_spec, key="secret")
    assert error.value.category == "invalid_request"
    async with db_session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(SelfHostedResourceOperation)) == 0
    agent = await agents.create(authorized, agent_spec(), key="valid")
    for patch in (
        AgentPatch(name="sh_reserved"),
        AgentPatch(name=None),
        AgentPatch(metadata={"api_key": "opaque-value"}),
    ):
        with pytest.raises(ProviderError) as error:
            await agents.update(
                authorized, agent.ref, patch, expected=agent.revision, key="invalid-patch"
            )
        assert error.value.category == "invalid_request"
    assert await agents.retrieve(authorized, agent.ref) == agent


@pytest.mark.parametrize("purge_first", [True, False])
async def test_account_erasure_serializes_resource_writes(
    db_committing_engine, monkeypatch, purge_first
):
    from daimon.core._models import Account, SelfHostedResourceOperation
    from daimon.core.purge import purge_account
    from daimon.core.stores import privacy_session_deletes, selfhosted_resources
    from sqlalchemy import func, select

    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    async with sessions.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
    authorized = scope(str(tenant.id), str(account.id).upper())
    agents = SelfHostedAgents(sessions)
    held, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    waiter_pid = None
    real_lock = privacy_session_deletes.lock_account

    async def hold_lock(session, *, account_id):
        await real_lock(session, account_id=account_id)
        held.set()
        await release.wait()

    async def wait_for_lock(session, *, account_id):
        nonlocal waiter_pid
        waiter_pid = await session.scalar(select(func.pg_backend_pid()))
        waiting.set()
        await real_lock(session, account_id=account_id)

    monkeypatch.setattr(
        privacy_session_deletes, "lock_account", hold_lock if purge_first else wait_for_lock
    )
    monkeypatch.setattr(
        selfhosted_resources, "lock_account", wait_for_lock if purge_first else hold_lock
    )

    async def write():
        return await agents.create(authorized, agent_spec(), key="concurrent")

    async def erase():
        return await purge_account(sm=sessions, account_id=account.id)

    first = asyncio.create_task(erase() if purge_first else write())
    second = None
    try:
        async with asyncio.timeout(10):
            await held.wait()
            second = asyncio.create_task(write() if purge_first else erase())
            await waiting.wait()
            async with sessions() as db:
                while not await db.scalar(
                    select(func.cardinality(func.pg_blocking_pids(waiter_pid)))
                ):
                    await asyncio.sleep(0.01)
        release.set()
        await first
        if purge_first:
            with pytest.raises(ScopeViolation, match="account no longer exists"):
                await second
        else:
            await second
        async with sessions() as db:
            assert await db.get(Account, account.id) is None
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(SelfHostedResourceOperation)
                    .where(SelfHostedResourceOperation.account_id == str(account.id))
                )
                == 0
            )
        with pytest.raises(ScopeViolation, match="account no longer exists"):
            await agents.create(authorized, agent_spec("after-erasure"), key="late")
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None), return_exceptions=True
        )
