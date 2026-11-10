"""`place_session`: the toolset and identity every `create_session` caller resolves first."""

from __future__ import annotations

from typing import cast

from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.agent_create_params import Tool
from daimon.core.config import Settings
from daimon.core.features.model_input import (
    TOOLSET_KEYS,
    SessionToolset,
    agent_builtins,
    agent_toolset,
)
from daimon.core.features.session_identity import IDENTITY_RESOLVE_KEYS, SessionIdentity
from daimon.core.features.snapshot import FeaturePlace, Sessions, resolve_features


async def place_toolset(
    sessions: Sessions,
    settings: Settings | None,
    place: FeaturePlace,
    agent: BetaManagedAgentsAgent,
) -> SessionToolset:
    """What the features at `place` change in `agent`'s tools for a new session."""
    return (await place_session(sessions, settings, place, agent))[0]


async def place_session(
    sessions: Sessions,
    settings: Settings | None,
    place: FeaturePlace,
    agent: BetaManagedAgentsAgent,
) -> tuple[SessionToolset, SessionIdentity]:
    """The toolset and identity features a new session of `agent` at `place` gets."""
    keys = tuple(dict.fromkeys((*TOOLSET_KEYS, *IDENTITY_RESOLVE_KEYS)))
    tools = cast(list[Tool], [tool.model_dump(mode="json") for tool in agent.tools])
    builtins = agent_builtins(tools)
    features = await resolve_features(sessions, settings, place, keys=keys, agent_builtins=builtins)
    return agent_toolset(features, agent), SessionIdentity.of(features)
