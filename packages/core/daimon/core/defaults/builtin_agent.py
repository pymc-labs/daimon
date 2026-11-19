"""Which agent a request for the built-in name reaches, one rule for every path.

A request for `default` reaches the agent of that name, a workspace's own
included. Only when nothing holds `default` does it fall back to the seeded
agent under its old name, until apply renames it.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, list_agents_by_tenant
from daimon.core.defaults.metadata import (
    BUILTIN_AGENT_NAME,
    LEGACY_BUILTIN_AGENT_NAME,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
)
from daimon.core.stores.agent_avatars import normalize_agent_name
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


def builtin_lookup_names(requested: str) -> tuple[str, ...]:
    """The names to look up for `requested`: empty unless it is the built-in name."""
    if normalize_agent_name(requested) != BUILTIN_AGENT_NAME:
        return ()
    return (BUILTIN_AGENT_NAME, LEGACY_BUILTIN_AGENT_NAME)


def resolve_builtin_agent_name(
    requested: str, live: Collection[str], *, tenant_id: uuid.UUID | str, surface: str
) -> str:
    """The name to resolve `requested` under.

    `live` holds the names from `builtin_lookup_names` that may answer: any
    agent named `default`, and the old name only where the seeded agent holds it.
    """
    if (
        not builtin_lookup_names(requested)
        or BUILTIN_AGENT_NAME in live
        or LEGACY_BUILTIN_AGENT_NAME not in live
    ):
        return requested
    _log.info(
        "builtin_agent.fallback_used",
        tenant_id=str(tenant_id),
        surface=surface,
        requested=requested,
        resolved=LEGACY_BUILTIN_AGENT_NAME,
    )
    return LEGACY_BUILTIN_AGENT_NAME


def route_builtin_agent(
    agents: Iterable[BetaManagedAgentsAgent], *, tenant_id: uuid.UUID, surface: str
) -> list[BetaManagedAgentsAgent]:
    """The seeded agents among a tenant's live `agents`, newest first: under the
    built-in name, else under the old one. Never a workspace's own agent."""
    seeded: dict[str, list[BetaManagedAgentsAgent]] = {
        name: [] for name in builtin_lookup_names(BUILTIN_AGENT_NAME)
    }
    for agent in agents:
        name = normalize_agent_name(agent.metadata.get(MA_METADATA_KEY_NAME, ""))
        if name in seeded and agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
            seeded[name].append(agent)
    target = resolve_builtin_agent_name(
        BUILTIN_AGENT_NAME,
        [name for name, found in seeded.items() if found],
        tenant_id=tenant_id,
        surface=surface,
    )
    return sorted(seeded[target], key=lambda agent: str(agent.created_at), reverse=True)


async def find_agent_or_builtin(
    client: AsyncAnthropic | None,
    *,
    tenant_id: uuid.UUID,
    name: str,
    records: async_sessionmaker[AsyncSession] | None,
    surface: str,
) -> BetaManagedAgentsAgent | None:
    """The live agent named `name`; for the built-in name, the seeded agent on a miss."""
    found = await find_agent_by_daimon_tag(client, tenant_id=tenant_id, name=name, records=records)
    if found is None and builtin_lookup_names(name):
        agents = await list_agents_by_tenant(client, tenant_id=tenant_id, records=records)
        found = next(iter(route_builtin_agent(agents, tenant_id=tenant_id, surface=surface)), None)
    return found
