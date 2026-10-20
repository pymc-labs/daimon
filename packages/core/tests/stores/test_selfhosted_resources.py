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
from daimon.testing.factories import make_tenant
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


def scope(tenant: str) -> Scope:
    return Scope(
        tenant_id=tenant, account_id=str(uuid4()), principal_id="operator", authorization_id="test"
    )


def agent_spec(name: str = "daimon") -> AgentSpec:
    return AgentSpec(
        name=name,
        model=ModelRef(provider=LOOP.provider, id="gpt-5"),
        system="Be helpful.",
        mcp_servers=(
            MCPConnection(
                name="tools", url="https://tools.example/mcp", credential_ref="MCP_TOKEN"
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
    other = await make_tenant(db_session)
    await db_session.commit()
    authorized = scope(str(tenant.id))
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
    await db_session.commit()
    authorized = scope(str(tenant.id))
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
    await db_session.commit()
    authorized = scope(str(tenant.id))
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
    authorized = scope(str(tenant.id))
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
