"""What an agent with a signed-identity MCP server attached may not do.

A server that opts into signed caller identity (`daimon.core.mcp_identity`)
answers with what the readers of the turn's channel can read. So a turn of such
an agent stays in that channel (it runs held, `daimon.core.turn_hold`) and the
agent keeps no long-term memory. An agent with no such server is untouched.
One predicate decides both: `agent_has_identity_relay`. Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Final

from daimon.core.features.snapshot import FeatureSnapshot
from daimon.core.mcp_identity import identity_relay_urls

#: Features that read or write long-term memory, forced off for these agents.
RELAY_AGENT_OFF_FEATURES: Final = (
    "memory_personal",
    "memory_shared",
    "memory_channel",
    "repo_memory",
)


def agent_has_identity_relay(server_urls: Iterable[str], *, public_url: str | None) -> bool:
    """Whether any of the agent's MCP server URLs is this deployment's identity relay."""
    if public_url is None:
        return False
    return bool(identity_relay_urls(server_urls, public_url=public_url))


def relay_seal_ids(*, channel_id: str, thread_id: str | None) -> frozenset[str]:
    """The ids a relay agent's session is sealed to: its channel, thread and Slack `channel:ts`
    (the same ids `daimon.core.permissions.limiting_ids_at` records for a limited channel)."""
    ids = (channel_id, thread_id, f"{channel_id}:{thread_id}" if thread_id is not None else None)
    return frozenset(i for i in ids if i is not None)


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
