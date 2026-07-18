"""Read + write helpers for the `agent_github_binding` table.

The overlay maps a per-agent UUID to a principal_id whose github_credentials
row holds the token. `get_pat(agent_id=X)` resolves tier-1 by reading this
table; if no row exists it returns None (no principal-default bleed on
the agent path).

`set_agent_github_binding` is the write path — UPSERT on agent_id PK.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from daimon.core._models import AgentGithubBinding
from daimon.core.stores.domain import AgentGithubBindingRow
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def get_agent_github_binding(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
) -> AgentGithubBindingRow | None:
    orm = await session.get(AgentGithubBinding, agent_id)
    if orm is None:
        return None
    return AgentGithubBindingRow.model_validate(orm)


async def set_agent_github_binding(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> AgentGithubBindingRow:
    """UPSERT the per-agent GitHub credential overlay.

    After this call, get_pat(agent_id=agent_id) resolves to the credential
    stored under principal_id. Per the per-agent model, callers set
    principal_id=agent_id so each agent has its own isolated credential row.
    """
    stmt = (
        pg_insert(AgentGithubBinding)
        .values(agent_id=agent_id, principal_id=principal_id)
        .on_conflict_do_update(
            index_elements=[AgentGithubBinding.agent_id],
            set_={"principal_id": principal_id},
        )
        .returning(AgentGithubBinding)
    )
    result = await session.execute(stmt)
    orm = result.scalar_one()
    return AgentGithubBindingRow.model_validate(orm)


async def delete_for_principal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> int:
    """Hard-delete every agent_github_binding row for a principal. Idempotent.

    Returns rows deleted; never raises on 0. Used by the GDPR purge
    orchestrator's per-principal walk — the credential-leak fix.

    principal_id is NOT the PK (agent_id is) and is non-unique, so one
    principal may back many agent bindings — rowcount is 0..N. Named
    delete_for_principal to match the identity_store / routines_store
    delete_for_principal family the orchestrator already dispatches.
    """
    result = await session.execute(
        delete(AgentGithubBinding).where(AgentGithubBinding.principal_id == principal_id)
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def count_for_principal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> int:
    """Read-only count of agent_github_binding rows for a principal.

    Used by the purge preview twin (privacy.py) to show what
    delete_for_principal would remove. Never mutates.
    """
    result = await session.execute(
        select(func.count())
        .select_from(AgentGithubBinding)
        .where(AgentGithubBinding.principal_id == principal_id)
    )
    return result.scalar_one()
