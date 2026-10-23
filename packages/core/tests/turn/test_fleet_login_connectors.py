"""An agent with a login-bearing connector runs a fleet; its workers never get the login."""

from __future__ import annotations

import json

import pytest
from daimon.core.neutral_resources import (
    WorkerSpecRefused,
    fleet_root_obstacle,
    resolve_worker_spec,
    task_worker_spec,
)
from daimon.core.turn.runtime_manifest import login_mcp_servers, worker_source_ceiling
from mux.contracts.resources import MCPConnection, ResourceBinding, ToolSpec

from ..test_agent_backend import owned as owned
from .test_fleet_source_permissions import NOW, compose
from .test_fleet_source_permissions import captured as captured

LOGIN = "vault:linear-login"


def with_login_connector(inputs, *, paired: bool = True):
    """The built-in Daimon's shape: a connector whose login the host projects."""
    source, *rest = inputs
    spec = source.effective_spec
    source = source.model_copy(
        update={
            "effective_spec": spec.model_copy(
                update={
                    "mcp_servers": (
                        *(spec.mcp_servers or ()),
                        MCPConnection(
                            name="linear", url="https://linear.invalid/mcp", credential_ref=LOGIN
                        ),
                    ),
                    "tools": (
                        *(spec.tools or ()),
                        ToolSpec(name="linear", kind="mcp_toolset"),
                    ),
                }
            ),
            "resource_requirements": (
                *source.resource_requirements,
                ResourceBinding(id=LOGIN, kind="credential", credential_ref=LOGIN)
                if paired
                else ResourceBinding(
                    id="vault:orphan", kind="credential", credential_ref="vault:orphan"
                ),
            ),
        }
    )
    return (source, *rest)


def mentions_login(value: object, login: str = LOGIN) -> bool:
    return login in json.dumps(value, default=str)


async def test_a_login_connector_is_not_a_fleet_obstacle(captured):
    inputs = with_login_connector(captured)
    source = inputs[0]
    assert login_mcp_servers(source) == {"linear"}
    assert fleet_root_obstacle(source) is None
    ceiling = worker_source_ceiling(source)
    assert "linear" not in {server.name for server in ceiling.effective_spec.mcp_servers or ()}
    assert "linear" not in {tool.name for tool in ceiling.effective_spec.tools or ()}
    assert "mcp" in ceiling.effective_spec.declared_drops
    assert not any(binding.credential_ref == LOGIN for binding in ceiling.resource_requirements)


async def test_the_root_freezes_without_the_connector_and_no_worker_gets_its_login(captured):
    inputs = with_login_connector(captured)
    source, _, grant, _, policy, _ = inputs
    root = compose(inputs)
    assert "linear" not in {server.name for server in root.spec.mcp_servers or ()}
    assert not any(binding.credential_ref == LOGIN for binding in root.resources)
    assert "mcp" in root.spec.declared_drops
    assert not mentions_login(root.spec.model_dump(mode="json"))
    assert not mentions_login([binding.model_dump(mode="json") for binding in root.resources])
    from daimon.core.neutral_resources import default_worker_limits

    for model in (None, *(model.id for model in grant.models)):
        spec = task_worker_spec(root, role="W", task="Compare X and Y.", model=model)
        assert "mcp" in spec.declared_drops
        child = resolve_worker_spec(
            root,
            spec,
            policy=policy,
            now=NOW,
            captured_source=source,
            resources=root.resources if spec.model.provider == "anthropic" else (),
            environment=root.environment if spec.model.provider == "anthropic" else None,
            limits=default_worker_limits(
                root.limits,
                None,
                (),
                provider=spec.model.provider,
                budget_usd=root.limits.budget_usd,
            ),
        )
        assert not mentions_login(child.spec.model_dump(mode="json"))
        assert not mentions_login([binding.model_dump(mode="json") for binding in child.resources])
        assert not any(binding.kind == "credential" for binding in child.resources)


async def test_a_clone_worker_can_not_put_the_connector_back(captured):
    inputs = with_login_connector(captured)
    source, _, _, _, policy, _ = inputs
    root = compose(inputs)
    widened = root.spec.model_copy(
        update={"mcp_servers": source.effective_spec.mcp_servers, "declared_drops": ()}
    )
    # A clone that keeps the ceiling's drops carries no connector at all.
    assert not root.spec.mcp_servers or all(
        server.credential_ref is None for server in root.spec.mcp_servers
    )
    assert "linear" not in {server.name for server in root.spec.mcp_servers or ()}
    with pytest.raises(WorkerSpecRefused):
        resolve_worker_spec(
            root,
            widened,
            policy=policy,
            now=NOW,
            captured_source=source,
            resources=(
                *root.resources,
                ResourceBinding(id=LOGIN, kind="credential", credential_ref=LOGIN),
            ),
            environment=root.environment,
            limits=root.limits,
        )


async def test_a_login_with_no_connector_still_refuses(captured):
    inputs = with_login_connector(captured, paired=False)
    assert fleet_root_obstacle(inputs[0]) == "credentials"
    with pytest.raises(WorkerSpecRefused):
        compose(inputs)


async def test_the_real_projectors_stored_logins_and_secrets_never_reach_a_worker(owned):
    """Built from project_session_resources: an external connector with a stored login
    (agent-mcp:<id>, which names no server), a stored .env secret and the caller login."""
    from cryptography.fernet import Fernet
    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.config import McpSettings
    from daimon.core.github_credentials import build_multifernet
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.mux_backend import resource_scope
    from daimon.core.sessions import project_session_resources
    from daimon.core.stores import agent_mcp_credentials, agent_memory_stores
    from daimon.core.stores.agent_files import put_agent_file
    from daimon.testing.factories import make_account
    from daimon.testing.ma_models import ma_agent, ma_environment
    from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
    from pydantic import HttpUrl, SecretStr

    service, identity, _, sessions = owned
    tenant_id = identity.tenant_id
    from daimon.core.stores.tenants import get_tenant

    async with sessions.begin() as db:
        account = await make_account(db, tenant=await get_tenant(db, tenant_id))
    native = ma_agent(
        id="builtin-daimon",
        name="daimon",
        tenant_id=str(tenant_id),
        model="claude-haiku-5-5",
        mcp_servers=[{"type": "url", "name": "linear", "url": "https://linear.example.test/mcp"}],
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": "linear",
                "configs": [],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            }
        ],
    )
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=native.id)
    async with sessions.begin() as db:
        stored = await agent_mcp_credentials.upsert_credential(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            mcp_server_url="https://linear.example.test/mcp",
            encrypted_token=b"private-ciphertext",
        )
        await put_agent_file(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            key="SERVICE_KEY",
            content="private-env-value",
            set_by_account_id=None,
        )
        await agent_memory_stores.insert_memory_store(
            db, tenant_id=tenant_id, agent_id=agent_id, memory_store_id="memstore-builtin"
        )
    scope = resource_scope(tenant_id=str(tenant_id), account_id=str(account.id))
    transport = ScriptedTransport()
    async with transport.client() as client:
        source = await load_backend_agent(
            client, sessions, tenant_id=tenant_id, agent_name="daimon", actual_agent=native
        )
        projected = await project_session_resources(
            source,
            agent=native,
            environment=ma_environment(tenant_id=str(tenant_id)),
            session_factory=sessions,
            tenant_id=tenant_id,
            agent_uuid=agent_id,
            account_id=account.id,
            resource_scope=scope,
            memory_read_only=True,
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            mcp_settings=McpSettings(
                public_url=HttpUrl("https://mcp.example.test/mcp"), jwt_secret=SecretStr("test")
            ),
        )
    logins = {
        binding.credential_ref
        for binding in projected.resource_requirements
        if binding.credential_ref is not None
    }
    stored_login = f"agent-mcp:{stored.id}"
    assert stored_login in logins and any(ref.startswith("agent-env:") for ref in logins)
    assert any(ref.startswith("caller-mcp:") for ref in logins)
    # Before: "credentials". Now the fleet is allowed and every login is left behind.
    assert fleet_root_obstacle(projected) is None
    ceiling = worker_source_ceiling(projected)
    dumped = json.dumps(
        [
            ceiling.effective_spec.model_dump(mode="json"),
            [binding.model_dump(mode="json") for binding in ceiling.resource_requirements],
        ]
    )
    for ref in logins:
        assert ref not in dumped
    assert "linear" not in {server.name for server in ceiling.effective_spec.mcp_servers or ()}
    assert {"mcp", "files"} <= set(ceiling.effective_spec.declared_drops)
    assert "private-ciphertext" not in dumped and "private-env-value" not in dumped
    assert not transport.violations
