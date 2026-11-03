"""Each agent's helper list: the agents it may ask inside its own turn."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable

from daimon.core._models import AgentHelper
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def list_helper_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: str
) -> list[str]:
    """The MA ids of `agent_id`'s helpers, oldest first."""
    rows = await session.execute(
        select(AgentHelper.helper_agent_id)
        .where(AgentHelper.tenant_id == tenant_id, AgentHelper.agent_id == agent_id)
        .order_by(AgentHelper.created_at, AgentHelper.helper_agent_id)
    )
    return list(rows.scalars())


async def list_helper_ids_many(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_ids: Iterable[str]
) -> dict[str, list[str]]:
    """Each of `agent_ids`' helper ids, oldest first; an agent with none is left out."""
    rows = await session.execute(
        select(AgentHelper.agent_id, AgentHelper.helper_agent_id)
        .where(AgentHelper.tenant_id == tenant_id, AgentHelper.agent_id.in_(list(agent_ids)))
        .order_by(AgentHelper.created_at, AgentHelper.helper_agent_id)
    )
    out: dict[str, list[str]] = {}
    for agent_id, helper_id in rows:
        out.setdefault(agent_id, []).append(helper_id)
    return out


async def set_helpers(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: str, helper_ids: Collection[str]
) -> None:
    """Make `helper_ids` the agent's whole list; kept entries keep their place."""
    await session.execute(
        delete(AgentHelper).where(
            AgentHelper.tenant_id == tenant_id,
            AgentHelper.agent_id == agent_id,
            AgentHelper.helper_agent_id.not_in(list(helper_ids)),
        )
    )
    wanted = [helper for helper in dict.fromkeys(helper_ids) if helper != agent_id]
    if wanted:
        await session.execute(
            insert(AgentHelper)
            .values(
                [
                    {"tenant_id": tenant_id, "agent_id": agent_id, "helper_agent_id": helper}
                    for helper in wanted
                ]
            )
            .on_conflict_do_nothing()
        )
