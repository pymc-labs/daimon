"""Per-message identity for a daimon agent."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.stores.agent_avatars import get_or_create_avatar
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class AgentIdentity:
    name: str
    avatar_url: str | None
    builtin: bool


def is_builtin_agent(
    *, name: str, metadata: Mapping[str, str] | None, default_agent_name: str | None
) -> bool:
    """Identify the managed deployment agent without assuming its display name."""
    return (metadata or {}).get(MA_METADATA_KEY_MANAGED) == "true" or (
        default_agent_name is not None and name == default_agent_name
    )


async def resolve_agent_identity(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    is_builtin: bool,
    public_base_url: str | None,
    enabled: bool = True,
) -> AgentIdentity:
    """Resolve the identity once when a turn admits an agent.

    Built-in turns keep the platform app's own name and icon.
    """
    if is_builtin or not enabled:
        return AgentIdentity(name=agent_name, avatar_url=None, builtin=True)
    avatar = await get_or_create_avatar(session, tenant_id=tenant_id, agent_name=agent_name)
    base = public_base_url.rstrip("/") if public_base_url else None
    url = f"{base}/avatars/{avatar.token}/{avatar.sha256[:12]}.png" if base else None
    return AgentIdentity(name=agent_name, avatar_url=url, builtin=False)
