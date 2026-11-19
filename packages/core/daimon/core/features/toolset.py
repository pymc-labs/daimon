"""`place_session`: the toolset and identity every `create_session` caller resolves first."""

from __future__ import annotations

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.config import Settings
from daimon.core.features.model_input import (
    TOOLSET_KEYS,
    SessionToolset,
    agent_toolset,
    builtins_of,
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
    *,
    server_setup: bool = False,
) -> tuple[SessionToolset, SessionIdentity]:
    """The toolset and identity features a new session of `agent` at `place` gets. Only the
    MCP server's own process passes `server_setup`: the setup is that server's (`SERVER_SETUP`)."""
    keys = tuple(dict.fromkeys((*TOOLSET_KEYS, *IDENTITY_RESOLVE_KEYS)))
    features = await resolve_features(
        sessions,
        settings,
        place,
        keys=keys,
        agent_builtins=builtins_of(agent),
        server_setup=server_setup,
    )
    return agent_toolset(features, agent), SessionIdentity.of(features)
