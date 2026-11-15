"""Agent records route between Daimon's tables and Managed Agents by tenant and id."""

from __future__ import annotations

import ast
import inspect
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.agent_record_ports import (
    RecordRoutedManagedAgents,
    RoutedAgents,
    RoutedEnvironments,
    RoutedResourceWalk,
)
from daimon.core.anthropic_guard import AnthropicDisabled, without_anthropic
from daimon.core.mux_backend import managed_agents, resource_ref
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.contracts.ids import ModelRef, PageRequest, Revision, Scope, SkillRef
from mux.contracts.ports import Agents, Environments
from mux.contracts.resources import (
    AgentFilter,
    AgentPatch,
    AgentSpec,
    EnvironmentFilter,
    EnvironmentPatch,
    EnvironmentSpec,
    MCPConnection,
    ToolSpec,
)
from mux.drivers.anthropic import (
    AGENT_VERSIONS_NAMESPACE,
    AgentVersions,
    AnthropicAgents,
    AnthropicEnvironments,
    EnvironmentReads,
    ResourceWalk,
)
from mux.errors import ProviderError
from mux.testing.drivers.anthropic.ma import build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PACKAGES = Path(__file__).resolve().parents[2]
# The one constructor and the routed ports it returns.
ALLOWED = {
    PACKAGES / "core/daimon/core/mux_backend.py",
    PACKAGES / "core/daimon/core/agent_record_ports.py",
}
CONSTRUCTORS = {"AnthropicManagedAgents", "AnthropicAgents", "AnthropicEnvironments"}


def _sources() -> list[Path]:
    roots = [PACKAGES / "core/daimon", *sorted((PACKAGES / "adapters").glob("*/daimon*"))]
    return [path for root in roots for path in root.rglob("*.py")]


def test_record_ports_have_one_constructor() -> None:
    """Host code reaches agent records only through `managed_agents`, never raw clients."""
    found: list[str] = []
    for path in _sources():
        if path in ALLOWED:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name in CONSTRUCTORS:
                    found.append(f"{path.relative_to(PACKAGES)}:{node.lineno} {name}(")
            if (
                isinstance(node, ast.Attribute)
                and node.attr in {"agents", "environments"}
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "beta"
            ):
                found.append(f"{path.relative_to(PACKAGES)}:{node.lineno} .beta.{node.attr}")
    assert len(_sources()) > 100, "the scan must see the host source trees"
    assert not found, f"agent records bypass the routed backend: {found}"


@pytest.mark.parametrize(
    ("routed", "native", "ports"),
    [
        (RoutedAgents, AnthropicAgents, (Agents, AgentVersions)),
        (RoutedEnvironments, AnthropicEnvironments, (Environments, EnvironmentReads)),
    ],
)
def test_every_record_port_method_is_routed(
    routed: type[object], native: type[object], ports: tuple[type[object], ...]
) -> None:
    """Each public MA method and each port method is overridden, so none skips routing."""
    public = {name for name, _ in inspect.getmembers(native, callable) if not name.startswith("_")}
    for port in ports:
        public |= {name for name in vars(port) if not name.startswith("_")}
    missing = sorted(name for name in public if name not in vars(routed))
    assert not missing, f"{routed.__name__} inherits unrouted methods {missing}"


def test_backend_extensions_serve_the_routed_ports() -> None:
    """Versions, environment reads and walks come from the composite, not the raw driver."""
    backend = managed_agents(build_fake_anthropic(_refuse), records=_unused_records())
    assert isinstance(backend, RecordRoutedManagedAgents), "records select the composite"
    assert isinstance(backend.agents, RoutedAgents), "agents are routed"
    assert isinstance(backend.environments, RoutedEnvironments), "environments are routed"
    versions = backend.extension(AgentVersions, namespace=AGENT_VERSIONS_NAMESPACE, version=1)
    reads = backend.extension(EnvironmentReads, namespace="anthropic.environment_reads", version=1)
    walk = backend.extension(ResourceWalk, namespace="anthropic.resource_walk", version=1)
    assert versions is backend.agents, "agent versions are routed"
    assert reads is backend.environments, "environment reads are routed"
    assert isinstance(walk, RoutedResourceWalk), "resource walks are routed"


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected Anthropic request {request.method} {request.url}")


def _unused_records() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker()


def _scope(tenant: str, account: str) -> Scope:
    return Scope(
        tenant_id=tenant, account_id=account, principal_id="operator", authorization_id="test"
    )


def _agent_spec(name: str = "researcher") -> AgentSpec:
    return AgentSpec(
        name=name,
        model=ModelRef(provider="openai", id="gpt-5"),
        system="Research carefully.",
        tools=(
            ToolSpec(name="bash", kind="builtin"),
            ToolSpec(name="search", kind="mcp_toolset", permission="ask"),
        ),
        mcp_servers=(MCPConnection(name="search", url="https://search.example/mcp"),),
        skills=(SkillRef(id="analysis", version="2"),),
    )


async def _tenant_scope(db_session: AsyncSession) -> Scope:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    return _scope(str(tenant.id), str(account.id))


async def test_daimon_record_needs_no_anthropic(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Create, read, version, list, walk and archive a Daimon record with Anthropic off."""
    scope = await _tenant_scope(db_session)
    client = build_fake_anthropic(_refuse)
    with without_anthropic():
        creator = managed_agents(client, scope=scope, records=db_session_factory)
        agent = await creator.agents.create_owned(scope, _agent_spec(), key="agent")
        environment = await creator.environments.create_owned(
            scope, EnvironmentSpec(name="cloud", execution="hosted"), key="env"
        )
        backend = managed_agents(
            client,
            scope=scope,
            resources=frozenset({("agent", agent.ref.id), ("environment", environment.ref.id)}),
            records=db_session_factory,
        )
        ref = resource_ref(backend, "agent", agent.ref.id, scope=scope)
        read = await backend.agents.retrieve(scope, ref)
        updated = await backend.agents.update(
            scope,
            ref,
            AgentPatch(system="Be brief."),
            expected=Revision(local=1, native="1"),
            key="update",
        )
        versions = backend.extension(AgentVersions, namespace=AGENT_VERSIONS_NAMESPACE, version=1)
        first = await versions.retrieve_version(scope, ref, 1)
        history = await backend.routed_agents.list_versions(scope, ref, page=PageRequest())
        env_ref = resource_ref(backend, "environment", environment.ref.id, scope=scope)
        native_env = await backend.extension(
            EnvironmentReads, namespace="anthropic.environment_reads", version=1
        ).retrieve_native(scope, env_ref)
        await backend.environments.update(
            scope, env_ref, EnvironmentPatch(description="d"), expected=Revision(local=0), key="e"
        )
        listed = await backend.agents.list(scope, filters=AgentFilter(), page=PageRequest())
        walk = backend.extension(ResourceWalk, namespace="anthropic.resource_walk", version=1)
        walked = [item async for item in walk.environments(scope, filters=EnvironmentFilter())]
        await backend.agents.archive(scope, ref, key="archive")
        with pytest.raises(AnthropicDisabled):
            await backend.agents.retrieve(
                scope, resource_ref(backend, "agent", "agent_remote", scope=scope)
            )
    facts = BetaManagedAgentsAgent.model_validate(read.native)
    assert facts.model.id == "gpt-5", f"model projected {facts.model}"
    assert [tool.type for tool in facts.tools] == ["agent_toolset_20260401", "mcp_toolset"], (
        f"tools projected {facts.tools}"
    )
    assert [skill.skill_id for skill in facts.skills] == ["analysis"], f"skills {facts.skills}"
    assert [server.url for server in facts.mcp_servers] == ["https://search.example/mcp"], (
        f"mcp projected {facts.mcp_servers}"
    )
    assert updated.revision.local == 2, f"update moves the revision {updated.revision}"
    assert first.native["system"] == "Research carefully.", f"version 1 {first.native}"
    assert [item.revision.local for item in history.data] == [2, 1], f"history {history}"
    assert native_env.native["config"] == {"type": "self_hosted"}, f"env facts {native_env}"
    assert [item.ref.id for item in listed.data] == [agent.ref.id], f"list {listed}"
    assert [item.ref.id for item in walked] == [environment.ref.id], f"walk {walked}"


async def test_tenant_b_cannot_read_tenant_a_record(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Another tenant's id is not found, even with a host grant, and never reaches MA."""
    owner = await _tenant_scope(db_session)
    other = await _tenant_scope(db_session)
    client = build_fake_anthropic(_refuse)
    creator = managed_agents(client, scope=owner, records=db_session_factory)
    agent = await creator.agents.create_owned(owner, _agent_spec(), key="agent")
    backend = managed_agents(
        client,
        scope=other,
        resources=frozenset({("agent", agent.ref.id)}),
        records=db_session_factory,
    )
    ref = resource_ref(backend, "agent", agent.ref.id, scope=other)
    for read in (
        backend.agents.retrieve(other, ref),
        backend.agents.retrieve_version(other, ref, 1),
        backend.agents.archive(other, ref, key="archive"),
    ):
        with pytest.raises(ProviderError) as refused:
            await read
        assert refused.value.category == "not_found", f"foreign record {refused.value}"
    with without_anthropic():
        listed = await backend.agents.list(other, filters=AgentFilter(), page=PageRequest())
    assert not listed.data, f"another tenant's records never list {listed}"


class _Trace:
    """Answer every MA agent and environment call, recording the exact request."""

    def __init__(self, tenant: str) -> None:
        self.tenant = tenant
        self.requests: list[tuple[str, str, list[tuple[str, str]], object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append(
            (request.method, request.url.path, list(request.url.params.multi_items()), body)
        )
        path = request.url.path
        if path.startswith("/v1/agents"):
            record = ma_agent(
                id="agent_remote",
                tenant_id=self.tenant,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ).model_dump(mode="json")
        else:
            record = ma_environment(id="env_remote", tenant_id=self.tenant).model_dump(mode="json")
        if request.method == "GET" and path in {"/v1/agents", "/v1/environments"}:
            return httpx.Response(200, json={"data": [record], "next_page": None})
        return httpx.Response(200, json=record)


async def _ma_calls(trace: _Trace, scope: Scope, records: async_sessionmaker[AsyncSession] | None):
    client = build_fake_anthropic(trace)
    resources = frozenset({("agent", "agent_remote"), ("environment", "env_remote")})
    backend = (
        managed_agents(client, scope=scope, resources=resources)
        if records is None
        else managed_agents(client, scope=scope, resources=resources, records=records)
    )
    ref = resource_ref(backend, "agent", "agent_remote", scope=scope)
    env_ref = resource_ref(backend, "environment", "env_remote", scope=scope)
    await backend.agents.retrieve(scope, ref)
    await backend.agents.update(
        scope, ref, AgentPatch(system="x"), expected=Revision(local=1, native="1"), key="u"
    )
    await backend.extension(
        AgentVersions, namespace=AGENT_VERSIONS_NAMESPACE, version=1
    ).retrieve_version(scope, ref, 1)
    await backend.agents.archive(scope, ref, key="a")
    await backend.agents.list(scope, filters=AgentFilter(), page=PageRequest(limit=5))
    await backend.environments.retrieve(scope, env_ref)
    await backend.extension(
        EnvironmentReads, namespace="anthropic.environment_reads", version=1
    ).retrieve_native(scope, env_ref)
    await backend.environments.list(scope, filters=EnvironmentFilter(), page=PageRequest())
    walk = backend.extension(ResourceWalk, namespace="anthropic.resource_walk", version=1)
    agents = [item.ref.id async for item in walk.agents(scope, filters=AgentFilter())]
    environments = [
        item.ref.id async for item in walk.environments(scope, filters=EnvironmentFilter())
    ]
    return agents, environments


async def test_ma_record_requests_are_unchanged(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """With records attached, MA calls send the same requests and merge in Daimon rows."""
    scope = await _tenant_scope(db_session)
    owned = await managed_agents(
        build_fake_anthropic(_refuse), scope=scope, records=db_session_factory
    ).agents.create_owned(scope, _agent_spec(), key="agent")
    before, after = _Trace(scope.tenant_id), _Trace(scope.tenant_id)
    plain = await _ma_calls(before, scope, None)
    routed = await _ma_calls(after, scope, db_session_factory)
    assert len(before.requests) == 10, f"every MA call was exercised {before.requests}"
    assert after.requests == before.requests, "records must not change MA requests"
    assert plain == (["agent_remote"], ["env_remote"]), f"plain walk {plain}"
    assert routed == (["agent_remote", owned.ref.id], ["env_remote"]), f"merged walk {routed}"
