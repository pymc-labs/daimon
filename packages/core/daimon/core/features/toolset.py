"""`place_toolset`: the session toolset every `create_session` caller resolves first."""

from __future__ import annotations

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.config import Settings
from daimon.core.features.model_input import SESSION_KEYS, SessionToolset, agent_toolset
from daimon.core.features.snapshot import FeaturePlace, Sessions, resolve_features


async def place_toolset(
    sessions: Sessions,
    settings: Settings | None,
    place: FeaturePlace,
    agent: BetaManagedAgentsAgent,
) -> SessionToolset:
    """What the features at `place` change in `agent`'s tools for a new session."""
    features = await resolve_features(sessions, settings, place, keys=SESSION_KEYS)
    return agent_toolset(features, agent)
