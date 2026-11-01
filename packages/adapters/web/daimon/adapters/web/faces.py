"""The face each agent shows in the web app: the avatar stored for it, when it has one."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.agent_view import load_avatar_urls


def avatar_base_url(runtime: WebRuntime) -> str | None:
    """Where stored avatars are served; None when this deployment serves none."""
    public_url = runtime.settings.mcp.public_url
    return str(public_url) if public_url is not None else None


async def avatars_of(
    runtime: WebRuntime, tenant_id: uuid.UUID, agents: Sequence[BetaManagedAgentsAgent]
) -> dict[str, str]:
    """Each agent's avatar URL, by agent id; an agent with none is left out."""
    base = avatar_base_url(runtime)
    if base is None or not agents:
        return {}
    async with runtime.sessionmaker() as db:
        return await load_avatar_urls(db, tenant_id=tenant_id, agents=agents, base_url=base)
