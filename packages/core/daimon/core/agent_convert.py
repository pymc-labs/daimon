"""Convert a Managed Agents agent into a Daimon-owned record on another provider.

Convert by delete: the record is created under the same name, then the MA twin
is archived through the raw port. Lifecycle archive is never used, because its
`forget_archived_agent` drops rules by name and would strip the new record's.
Managed Agents has no hard delete, so the raw archive is the delete.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from uuid import uuid4

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.agent_identity import queue_agent_face
from daimon.core.agent_lifecycle import strip_credentialed_mcp_servers
from daimon.core.agent_ownership import agent_name_lock, record_agent_creator
from daimon.core.agent_record_ports import DAIMON_ID_PREFIXES
from daimon.core.anthropic_guard import require_anthropic
from daimon.core.authz import Subject
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.owned import create_owned_record, keyless_provider, owned_scope
from daimon.core.errors import UserFacingError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from daimon.core.mux_compat import legacy_call, source_agent_record
from daimon.core.stores import agent_owners
from mux.contracts.ids import ModelRef, Provider
from mux.profiles import selectable_models
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ConvertedAgent:
    """The new record and the skill names it could not carry."""

    agent: BetaManagedAgentsAgent
    dropped_skills: tuple[str, ...]


async def convert_agent_to_daimon(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    subject: Subject,
    provider: Provider | None = None,
) -> ConvertedAgent:
    """Move the MA agent `name` to a Daimon record on `provider` (default: first keyed)."""
    if not subject.is_admin:
        raise UserFacingError("Only an admin can convert an agent.")
    require_anthropic(anthropic)
    target = provider or keyless_provider()
    async with agent_name_lock(sessionmaker, tenant_id=tenant_id, name=name):
        source = await find_agent_by_daimon_tag(
            anthropic, tenant_id=tenant_id, name=name, records=sessionmaker
        )
        if source is None:
            raise UserFacingError(f"No agent named {name}.")
        if source.id.startswith(DAIMON_ID_PREFIXES):
            raise UserFacingError(f"{name} already runs on Daimon.")
        scope = resource_scope(tenant_id=str(tenant_id))
        projected = source_agent_record(source, scope)
        if projected is None:
            raise UserFacingError(f"{name} can't be converted.")
        spec = projected.spec
        servers, _ = await strip_credentialed_mcp_servers(
            sessionmaker=sessionmaker,
            tenant_id=tenant_id,
            source_agent_uuid=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=source.id),
            mcp_servers=[
                {"type": "url", "name": s.name, "url": s.url} for s in spec.mcp_servers or ()
            ],
            tools=None,
        )
        kept = {str(server["name"]) for server in servers or ()}
        record = spec.model_copy(
            update={
                "model": ModelRef(provider=target, id=selectable_models(target)[0]),
                "mcp_servers": tuple(s for s in spec.mcp_servers or () if s.name in kept) or None,
                # The target host supplies its own tools; Claude skills and model
                # config have no meaning there.
                "tools": None,
                "skills": None,
                "extensions": None,
            }
        )
        created = await create_owned_record(
            sessionmaker,
            type(spec).model_validate(record.model_dump(mode="json")),
            scope=owned_scope(tenant_id, None),
            tenant_id=tenant_id,
        )
        await _carry_owner(sessionmaker, tenant_id=tenant_id, source_id=source.id, copy=created)
        backend = managed_agents(
            anthropic, scope=scope, resources=frozenset({("agent", source.id)})
        )
        await legacy_call(
            backend.agents.archive(
                scope, resource_ref(backend, "agent", source.id, scope=scope), key=str(uuid4())
            )
        )
    queue_agent_face(
        sessionmaker,
        tenant_id=tenant_id,
        agent_name=name,
        metadata=created.metadata,
        default_agent_name=None,
    )
    dropped = tuple(skill.skill_id for skill in source.skills or ())
    _log.info(
        "agent.converted", name=name, source_id=source.id, agent_id=created.id, provider=target
    )
    return ConvertedAgent(created, dropped)


async def _carry_owner(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    source_id: str,
    copy: BetaManagedAgentsAgent,
) -> None:
    async with sessionmaker() as session:
        owner = await agent_owners.owner_of(
            session, tenant_id, derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=source_id)
        )
    if owner is not None:
        await record_agent_creator(
            sessionmaker,
            tenant_id=tenant_id,
            agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=copy.id),
            account_id=owner,
        )
