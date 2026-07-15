"""Read-only store for agent_google_binding (Phase 19, GH-03).

Day-1 read-only: Phase 25's agent-setup panel will add upsert/delete
helpers. The GWS token-broker provider only needs to read the binding.
"""

from __future__ import annotations

import uuid

from daimon.core._models import AgentGoogleBinding
from daimon.core.stores.domain import AgentGoogleBindingRow
from sqlalchemy.ext.asyncio import AsyncSession


async def get_agent_google_binding(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
) -> AgentGoogleBindingRow | None:
    """Return the per-agent Google binding, or None when unbound."""
    orm = await session.get(AgentGoogleBinding, agent_id)
    if orm is None:
        return None
    return AgentGoogleBindingRow.model_validate(orm)
