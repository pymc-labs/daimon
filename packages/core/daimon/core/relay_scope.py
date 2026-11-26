"""What an agent with a signed-identity MCP server attached may not do.

A server that opts into signed caller identity (`daimon.core.mcp_identity`)
answers with what the readers of the turn's channel can read. So a turn of such
an agent stays in that channel (it runs held, `daimon.core.turn_hold`) and the
agent keeps no long-term memory. An agent with no such server is untouched, as is
one whose servers all set `channel_scoped=false` (they check each call's asker and
place themselves).
One predicate decides both: `agent_has_identity_relay`. Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import TYPE_CHECKING, Final

from daimon.core.data_tools import DATA_READS, DATA_SOURCES, PERSONAL_PARTS
from daimon.core.features.session_identity import SessionIdentity
from daimon.core.features.snapshot import FeatureSnapshot
from daimon.core.mcp_identity import identity_relay_urls, relay_base
from daimon.core.mcp_personal_servers import visible_mcp_servers, visible_tools
from daimon.core.mcp_server_url import canonical_mcp_url

if TYPE_CHECKING:
    from anthropic.types.beta import BetaManagedAgentsAgent

#: Features that read or write long-term memory, forced off for these agents.
RELAY_AGENT_OFF_FEATURES: Final = (
    "memory_personal",
    "memory_shared",
    "memory_channel",
    "repo_memory",
)


def agent_has_identity_relay(
    server_urls: Iterable[str],
    *,
    public_url: str | None,
    unscoped: frozenset[str],
) -> bool:
    """Whether any of the agent's MCP server URLs is a channel-scoped identity relay.

    `unscoped` names the servers that set `channel_scoped=false`; a relay URL naming
    any other server, configured or not, restricts the agent.
    """
    if public_url is None:
        return False
    base = relay_base(public_url)
    return any(
        canonical_mcp_url(url)[len(base) :] not in unscoped
        for url in identity_relay_urls(server_urls, public_url=public_url)
    )


def identity_relay_server_names(
    agent: BetaManagedAgentsAgent, *, public_url: str | None
) -> frozenset[str]:
    """The names of the agent's signed-identity servers, scoped or not.

    A contained room leaves all of them out (`without_identity_relays`), including the ones
    that set `channel_scoped=false`: there is no reader list to cut their answers to.
    """
    if public_url is None:
        return frozenset()
    relay = {
        canonical_mcp_url(url)
        for url in identity_relay_urls(
            (server.url for server in agent.mcp_servers), public_url=public_url
        )
    }
    return frozenset(
        server.name for server in agent.mcp_servers if canonical_mcp_url(server.url) in relay
    )


def without_identity_relays(
    agent: BetaManagedAgentsAgent, *, public_url: str | None
) -> tuple[BetaManagedAgentsAgent, frozenset[str]]:
    """The agent without its signed-identity servers and their toolsets, and their names."""
    names = identity_relay_server_names(agent, public_url=public_url)
    if not names:
        return agent, names
    stripped = agent.model_copy(
        update={
            "mcp_servers": list(visible_mcp_servers(agent, names)),
            "tools": list(visible_tools(agent, names)),
        }
    )
    return stripped, names


def relay_seal_ids(*, channel_id: str, thread_id: str | None) -> frozenset[str]:
    """The ids a relay agent's session is sealed to: its channel, thread and Slack `channel:ts`
    (the same ids `daimon.core.permissions.limiting_ids_at` records for a limited channel)."""
    ids = (channel_id, thread_id, f"{channel_id}:{thread_id}" if thread_id is not None else None)
    return frozenset(i for i in ids if i is not None)


def relay_memory_is_on(identity: SessionIdentity) -> bool:
    """Whether a session built with `identity` mounts any long-term memory."""
    return identity.personal_memory or identity.shared_memory or identity.channel_memory


def without_long_term_memory(snapshot: FeatureSnapshot) -> FeatureSnapshot:
    """The snapshot with every long-term memory feature Off and locked.

    `ResolvedFeature` is imported lazily: the resolver needs the registry.
    """
    from daimon.core.features.resolver import ResolvedFeature

    forced = {
        key: ResolvedFeature(key, "off", "agent", locked=True)
        for key in RELAY_AGENT_OFF_FEATURES
        if key in snapshot.features
    }
    return replace(snapshot, features={**snapshot.features, **forced})


def without_data_reads(snapshot: FeatureSnapshot) -> FeatureSnapshot:
    """The snapshot with `data_reads` Off and locked: a room outsiders may read, or a turn held
    for an outsider, is offered no data tool (`daimon.core.data_audience`). A person's own
    Drive stays: only they can press its card, which says the room sees the result."""
    from daimon.core.features.resolver import ResolvedFeature

    if DATA_READS not in snapshot.features:
        return snapshot
    forced = ResolvedFeature(DATA_READS, "off", "agent", locked=True)
    return replace(snapshot, features={**snapshot.features, DATA_READS: forced})


def without_data_sources(snapshot: FeatureSnapshot) -> FeatureSnapshot:
    """The snapshot with `data_sources` and `data_reads` Off: a workspace with no source, or
    no ready store, is offered no data tool."""
    from daimon.core.features.resolver import ResolvedFeature

    forced = ResolvedFeature(DATA_SOURCES, "off", "workspace", locked=True)
    reads = without_data_reads(snapshot)
    return replace(reads, features={**reads.features, DATA_SOURCES: forced})


def without_personal_google(snapshot: FeatureSnapshot) -> FeatureSnapshot:
    """The snapshot with the personal part features Off and locked: a workspace with no Google
    source has nothing to read a person's Drive or calendar through."""
    from daimon.core.features.resolver import ResolvedFeature

    forced = {
        key: ResolvedFeature(key, "off", "workspace", locked=True)
        for key in PERSONAL_PARTS.values()
        if key in snapshot.features
    }
    return replace(snapshot, features={**snapshot.features, **forced})
