"""Store for web_agent_grants: an agent's web chat switch and who it serves."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from daimon.core._models import WebAgentGrant
from daimon.core.stores.domain import WebAgentGrantRow, WebAgentWho
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def list_grants(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> dict[str, WebAgentGrantRow]:
    """Grants by agent id. An agent with none is on, for everyone."""
    rows = await session.scalars(select(WebAgentGrant).where(WebAgentGrant.tenant_id == tenant_id))
    return {r.agent_id: WebAgentGrantRow.model_validate(r) for r in rows}


async def set_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: str,
    web: bool,
    who: WebAgentWho,
    groups: Sequence[str],
    now: datetime,
) -> WebAgentGrantRow:
    values = {"web": web, "who": who, "groups": list(dict.fromkeys(groups)), "updated_at": now}
    orm = await session.scalar(
        insert(WebAgentGrant)
        .values(tenant_id=tenant_id, agent_id=agent_id, **values)
        .on_conflict_do_update(
            index_elements=[WebAgentGrant.tenant_id, WebAgentGrant.agent_id], set_=values
        )
        .returning(WebAgentGrant)
    )
    assert orm is not None, "an upsert returns its row"
    return WebAgentGrantRow.model_validate(orm)
