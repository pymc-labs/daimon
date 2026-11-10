"""Which agent a request for the built-in name reaches, one rule for every path.

Until apply renames the seeded `daimon` to `default`, a request for `default`
reaches the seeded `daimon`. A workspace's own unmanaged `default` is never
taken for the built-in: with the seeded one present the request falls back to
it, and without it the request misses.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping

import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.defaults.metadata import (
    BUILTIN_AGENT_NAME,
    LEGACY_AGENT_RENAMES,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
)
from daimon.core.stores.agent_avatars import normalize_agent_name

_log = structlog.get_logger(__name__)


def builtin_lookup_names(requested: str) -> tuple[str, ...]:
    """The names to look up for `requested`: empty unless it is the built-in name."""
    if normalize_agent_name(requested) != BUILTIN_AGENT_NAME:
        return ()
    return (
        BUILTIN_AGENT_NAME,
        *(old for old, new in LEGACY_AGENT_RENAMES.items() if new == BUILTIN_AGENT_NAME),
    )


def resolve_builtin_agent_name(
    requested: str, holders: Mapping[str, bool], *, tenant_id: uuid.UUID | str, surface: str
) -> str | None:
    """The name to resolve `requested` under; None when it must miss.

    `holders` maps each live name from `builtin_lookup_names` to whether the
    seeded (managed) agent holds it. A name nobody holds is absent.
    """
    if not builtin_lookup_names(requested):
        return requested
    current = holders.get(BUILTIN_AGENT_NAME)
    if current:
        return requested
    for old, new in LEGACY_AGENT_RENAMES.items():
        if new == BUILTIN_AGENT_NAME and holders.get(old):
            collision = current is False
            (_log.warning if collision else _log.info)(
                "builtin_agent.fallback_used",
                tenant_id=str(tenant_id),
                surface=surface,
                requested=requested,
                resolved=old,
                reason="collision" if collision else "missing",
            )
            return old
    if current is False:
        _log.warning(
            "builtin_agent.unmanaged_refused",
            tenant_id=str(tenant_id),
            surface=surface,
            requested=requested,
        )
        return None
    return requested


def route_builtin_agent(
    agents: Iterable[BetaManagedAgentsAgent], *, tenant_id: uuid.UUID, surface: str
) -> list[BetaManagedAgentsAgent] | None:
    """The seeded agents a request for the built-in name reaches among a tenant's
    live `agents`, newest first; None when it must miss."""
    seeded: dict[str, list[BetaManagedAgentsAgent]] = {
        name: [] for name in builtin_lookup_names(BUILTIN_AGENT_NAME)
    }
    holders: dict[str, bool] = {}
    for agent in agents:
        name = agent.metadata.get(MA_METADATA_KEY_NAME)
        if name in seeded:
            managed = agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
            holders[name] = holders.get(name, False) or managed
            if managed:
                seeded[name].append(agent)
    target = resolve_builtin_agent_name(
        BUILTIN_AGENT_NAME, holders, tenant_id=tenant_id, surface=surface
    )
    if target is None:
        return None
    return sorted(seeded[target], key=lambda agent: str(agent.created_at), reverse=True)
