"""Resolve tenant-owned resources without provider I/O.

Admission still carries the historical SDK-shaped policy facts. These local
projections keep existing policy and billing callers working; the self-hosted
driver reads the full mux records through its injected resource ports.
"""

from __future__ import annotations

from uuid import UUID

from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.stores.selfhosted_resources import SelfHostedAgents, SelfHostedEnvironments
from mux.contracts.ids import ResourceRef, Scope
from mux.errors import ProviderError
from mux.profiles.selfhosted import LOOP
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def resolve_records(
    sessionmaker: async_sessionmaker[AsyncSession],
    scope: Scope,
    *,
    agent_name: str,
    environment_name: str,
    responder_id: str | None,
) -> tuple[BetaManagedAgentsAgent, BetaEnvironment]:
    try:
        agents = SelfHostedAgents(sessionmaker)
        agent = (
            await agents.retrieve(
                scope,
                ResourceRef(
                    id=responder_id,
                    kind="agent",
                    provider=LOOP.provider,
                    tenant_id=scope.tenant_id,
                    account_scope_id=scope.tenant_id,
                ),
            )
            if responder_id is not None
            else await agents.resolve(scope, agent_name)
        )
    except ProviderError as error:
        if error.category != "not_found":
            raise
        raise MAResolverMissError(
            kind="agent", tenant_id=UUID(scope.tenant_id), daimon_tag=agent_name
        ) from error
    try:
        environment = await SelfHostedEnvironments(sessionmaker).resolve(scope, environment_name)
    except ProviderError as error:
        if error.category != "not_found":
            raise
        raise MAResolverMissError(
            kind="environment", tenant_id=UUID(scope.tenant_id), daimon_tag=environment_name
        ) from error
    agent_facts = BetaManagedAgentsAgent.model_validate(
        {
            "id": agent.ref.id,
            "type": "agent",
            "name": agent.spec.name,
            "version": agent.revision.local,
            "model": {"id": agent.spec.model.id},
            "system": agent.spec.system,
            "description": agent.spec.description,
            "metadata": {
                **dict(agent.spec.metadata or {}),
                MA_METADATA_KEY_NAME: agent.spec.name,
                MA_METADATA_KEY_TENANT: scope.tenant_id,
            },
            "tools": [],
            "mcp_servers": [],
            "skills": [],
            "created_at": agent.created_at,
            "updated_at": agent.updated_at,
            "archived_at": agent.archived_at,
        }
    )
    environment_facts = BetaEnvironment.model_validate(
        {
            "id": environment.ref.id,
            "type": "environment",
            "name": environment.spec.name,
            "description": environment.spec.description or "",
            "metadata": {
                **dict(environment.spec.metadata or {}),
                MA_METADATA_KEY_NAME: environment.spec.name,
                MA_METADATA_KEY_TENANT: scope.tenant_id,
            },
            "config": {"type": "self_hosted"},
            "created_at": environment.created_at.isoformat(),
            "updated_at": environment.updated_at.isoformat() if environment.updated_at else None,
            "archived_at": environment.archived_at.isoformat() if environment.archived_at else None,
        }
    )
    return agent_facts, environment_facts
