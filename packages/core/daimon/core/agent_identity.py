"""Per-message identity for a daimon agent."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class AgentIdentity:
    name: str
    avatar_url: str | None
    builtin: bool


async def resolve_agent_identity(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    is_builtin: bool,
    public_base_url: str | None,
) -> AgentIdentity:
    """Resolve the identity once when a turn admits an agent.

    Built-in turns keep the platform app's own name and icon. Avatar lookup
    and lazy default creation are added behind this interface.
    """
    if is_builtin:
        return AgentIdentity(name=agent_name, avatar_url=None, builtin=True)
    return AgentIdentity(name=agent_name, avatar_url=None, builtin=False)
