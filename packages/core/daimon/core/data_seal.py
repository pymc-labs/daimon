"""Whether an agent reads data sources at a place: the one read every sealing path asks.

An agent with `data_reads` On is sealed by the relay rule (`daimon.core.relay_scope`): a
fresh session per channel turn, no long-term memory, held to its room. Other agents may
not ask it (helpers, agent chat, handoff) unless they read data in the same room. The
read resolves `data_reads` with its dependencies; a failed read raises, so a caller
refuses rather than guess.
"""

from __future__ import annotations

from typing import Final

from daimon.core.config import Settings
from daimon.core.data_tools import DATA_READS
from daimon.core.features.snapshot import FeaturePlace, Sessions, resolve_features
from daimon.core.ports.gate import data_sources_on

AGENT_REQUEST_REFUSAL: Final = (
    "That agent reads data sources, so only its own conversations can ask it. "
    "Ask it directly in this conversation instead."
)


async def reads_data(sessions: Sessions, settings: Settings | None, place: FeaturePlace) -> bool:
    """Whether the agent `place` names reads data sources there; no read while the deployment
    has data sources off (`data_reads` depends on them)."""
    if settings is not None and not data_sources_on(settings):
        return False
    return (await resolve_features(sessions, settings, place, keys=(DATA_READS,))).on(DATA_READS)
