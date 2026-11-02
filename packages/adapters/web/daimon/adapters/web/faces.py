"""The face each agent shows in the web app: the avatar stored for it, when it has one."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.agent_view import load_avatar_urls
from daimon.core.config import origin_of


def avatar_base_url(runtime: WebRuntime) -> str:
    """Where stored avatars are served: this app's own origin, which its image policy allows."""
    return origin_of(str(runtime.web.public_url))


async def avatars_of(
    runtime: WebRuntime, tenant_id: uuid.UUID, agents: Sequence[BetaManagedAgentsAgent]
) -> dict[str, str]:
    """Each agent's avatar URL, by agent id; an agent with none is left out."""
    base = avatar_base_url(runtime)
    if not agents:
        return {}
    async with runtime.sessionmaker() as db:
        return await load_avatar_urls(db, tenant_id=tenant_id, agents=agents, base_url=base)
