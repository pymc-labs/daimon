"""Convert an agent between Managed Agents and a Daimon-owned record, either way.

Convert by delete: the new agent is created under the same name, every row keyed
by the old one moves to it in one transaction (`stores.agent_rekey`), then the old
one is archived through the raw port. Lifecycle archive is never used, because its
`forget_archived_agent` drops rules by name and would strip the new agent's.
Each step is safe to repeat, so a re-run after a crash finishes the move.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from uuid import uuid4

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsURLMCPServerParams
from daimon.core.agent_identity import queue_agent_face
from daimon.core.agent_lifecycle import strip_credentialed_mcp_servers
from daimon.core.agent_ownership import agent_name_lock
from daimon.core.agent_record_ports import DAIMON_ID_PREFIXES
from daimon.core.anthropic_guard import require_anthropic
from daimon.core.authz import Subject
from daimon.core.constants import DEFAULT_AGENT_MODEL
from daimon.core.defaults.ma_index import find_agents_by_daimon_tag
from daimon.core.defaults.mcp_merge import merge_default_mcp_server, merge_default_mcp_toolset
from daimon.core.defaults.metadata import MA_METADATA_KEY_CONVERTED_FROM
from daimon.core.defaults.owned import create_owned_record, keyless_provider, owned_scope
from daimon.core.errors import UserFacingError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from daimon.core.mux_compat import create_agent, legacy_call, source_agent_record
from daimon.core.specs import merge_default_agent_toolset
from daimon.core.stores.agent_rekey import open_agent_work, rekey_agent
from daimon.core.stores.selfhosted_resources import SelfHostedAgents
from mux.contracts.ids import ModelRef, Provider
from mux.contracts.resources import Agent
from mux.errors import ProviderError
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
        tagged = await find_agents_by_daimon_tag(
            anthropic, tenant_id=tenant_id, name=name, records=sessionmaker
        )
        # A record from a crashed run shares the name; the MA agent is still the source.
        source = next((a for a in tagged if not a.id.startswith(DAIMON_ID_PREFIXES)), None)
        if source is None:
            if tagged:
                raise UserFacingError(f"{name} already runs on Daimon.")
            raise UserFacingError(f"No agent named {name}.")
        await _refuse_open_work(sessionmaker, tenant_id=tenant_id, name=name, agent_id=source.id)
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
                # Lets a token signed for the old id keep working (see the MCP verifier).
                "metadata": dict(spec.metadata or {}) | {MA_METADATA_KEY_CONVERTED_FROM: source.id},
            }
        )
        created = await create_owned_record(
            sessionmaker,
            type(spec).model_validate(record.model_dump(mode="json")),
            scope=owned_scope(tenant_id, None),
            tenant_id=tenant_id,
            key=f"convert:{source.id}",
        )
        await _move_rows(
            sessionmaker, tenant_id=tenant_id, name=name, old_id=source.id, new_id=created.id
        )
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


async def convert_agent_to_anthropic(
    anthropic: AsyncAnthropic | None,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    subject: Subject,
    public_url: str | None,
) -> ConvertedAgent:
    """Move the Daimon record `name` back to a Managed Agents agent on Claude.

    The new agent carries a marker naming the record, so a re-run after a crash
    adopts it instead of creating a second one.
    """
    if not subject.is_admin:
        raise UserFacingError("Only an admin can convert an agent.")
    require_anthropic(anthropic)
    scope = resource_scope(tenant_id=str(tenant_id))
    async with agent_name_lock(sessionmaker, tenant_id=tenant_id, name=name):
        try:
            record = await SelfHostedAgents(sessionmaker).resolve(scope, name)
        except ProviderError as exc:
            if exc.category != "not_found":
                raise
            raise UserFacingError(f"No Daimon agent named {name}.") from exc
        await _refuse_open_work(
            sessionmaker, tenant_id=tenant_id, name=name, agent_id=record.ref.id
        )
        tagged = await find_agents_by_daimon_tag(anthropic, tenant_id=tenant_id, name=name)
        created = next(
            (a for a in tagged if a.metadata.get(MA_METADATA_KEY_CONVERTED_FROM) == record.ref.id),
            None,
        )
        if created is None:
            created = await create_agent(
                anthropic, _anthropic_payload(record, public_url=public_url), scope=scope
            )
        await _move_rows(
            sessionmaker, tenant_id=tenant_id, name=name, old_id=record.ref.id, new_id=created.id
        )
        records, writer = SelfHostedAgents(sessionmaker), owned_scope(tenant_id, None)
        await records.archive(
            writer, records.record_ref(writer, record.ref.id), key=f"convert:{record.ref.id}"
        )
    queue_agent_face(
        sessionmaker,
        tenant_id=tenant_id,
        agent_name=name,
        metadata=created.metadata,
        default_agent_name=None,
    )
    # Daimon skills are not Claude skills; the person adds them back by name.
    dropped = tuple(skill.id for skill in record.spec.skills or ())
    _log.info(
        "agent.converted",
        name=name,
        source_id=record.ref.id,
        agent_id=created.id,
        provider="anthropic",
    )
    return ConvertedAgent(created, dropped)


def _anthropic_payload(record: Agent, *, public_url: str | None) -> dict[str, object]:
    servers: list[BetaManagedAgentsURLMCPServerParams] = [
        {"type": "url", "name": server.name, "url": server.url}
        for server in record.spec.mcp_servers or ()
    ]
    return {
        "name": record.spec.name,
        "description": record.spec.description,
        "system": record.spec.system,
        "model": DEFAULT_AGENT_MODEL,
        "mcp_servers": merge_default_mcp_server(servers or None, public_url),
        # The record's tools are the other host's; Claude gets its own base set.
        "tools": merge_default_agent_toolset(merge_default_mcp_toolset(None, public_url)),
        "metadata": dict(record.spec.metadata or {})
        | {MA_METADATA_KEY_CONVERTED_FROM: record.ref.id},
    }


async def _refuse_open_work(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    agent_id: str,
) -> None:
    async with sessionmaker() as session:
        if await open_agent_work(session, tenant_id=tenant_id, agent_id=agent_id):
            raise UserFacingError(f"{name} is still working. Try again when it's done.")


async def _move_rows(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    old_id: str,
    new_id: str,
) -> None:
    # Checked again inside the move: a wake queued since the first check would
    # otherwise fire at an agent that no longer exists.
    async with sessionmaker() as session, session.begin():
        if await open_agent_work(session, tenant_id=tenant_id, agent_id=old_id):
            raise UserFacingError(f"{name} is still working. Try again when it's done.")
        moved = await rekey_agent(session, tenant_id=tenant_id, old_id=old_id, new_id=new_id)
    _log.info("agent.rekeyed", name=name, old_id=old_id, new_id=new_id, rows=moved)
