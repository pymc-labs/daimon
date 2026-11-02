"""Ordinary agent source facts for backend conversion and consent tests."""

from datetime import UTC, datetime
from uuid import uuid4

from daimon.core.stores.domain import BackendAgent
from mux.contracts.ids import ModelRef, ResourceRef, Revision, Scope
from mux.contracts.resources import (
    Agent,
    AgentSpec,
    Environment,
    EnvironmentSpec,
    NetworkPolicy,
    ToolSpec,
)


def captured_source():
    now = datetime(2026, 10, 12, 18, tzinfo=UTC)
    tenant, account, agent_id = uuid4(), uuid4(), uuid4()
    scope = Scope(
        tenant_id=str(tenant),
        account_id=str(account),
        principal_id="caller",
        authorization_id="admitted-source",
    )
    spec = AgentSpec(
        name="root",
        model=ModelRef(provider="anthropic", id="claude-haiku-5-5"),
        system="Protected root instruction",
        tools=(
            ToolSpec(
                name="helper", kind="custom", input_schema={"type": "object", "properties": {}}
            ),
        ),
        skills=(),
    )
    source = Agent(
        ref=ResourceRef(
            provider="anthropic",
            kind="agent",
            id="captured-source",
            account_scope_id="owned-workspace",
            tenant_id=str(tenant),
            account_id=str(account),
        ),
        revision=Revision(local=1, native="1"),
        spec=spec,
        native={"id": "captured-source", "version": 1},
        created_at=now,
    )
    environment = Environment(
        ref=source.ref.model_copy(update={"kind": "environment", "id": "environment"}),
        revision=Revision(local=1, native="1"),
        spec=EnvironmentSpec(
            name="root-box",
            network=NetworkPolicy(mode="limited", allowed_hosts=("allowed.invalid",)),
        ),
        created_at=now,
    )
    return BackendAgent(
        tenant_id=tenant,
        agent_id=agent_id,
        name="source",
        configuration_digest="never-authority",
        source=source,
        effective_spec=spec,
        resource_scope=scope,
        environment=environment,
        memory_required=False,
        memory_read_only=True,
    )
