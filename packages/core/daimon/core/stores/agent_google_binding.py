"""Store for agent_google_binding.

Holds the operator-configured Google Workspace identity overlay for an
agent: which email and scope set the token broker impersonates via
domain-wide delegation against the tenant service account. Written by
the admin CLI (`daimon agents bind-google`); read by the GWS token
broker provider.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from daimon.core._models import AgentGoogleBinding
from daimon.core.errors import StoreError
from daimon.core.stores.domain import AgentGoogleBindingRow
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
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


async def upsert_agent_google_binding(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    email: str,
    scopes: Sequence[str],
) -> AgentGoogleBindingRow:
    """Upsert the Google identity overlay for `agent_id`. Last-write-wins.

    Returns the post-write row. Uses `.returning(...)` + `scalar_one()` so the
    cursor — not the identity map — is the source of truth, mirroring
    `agent_files.put_agent_file`.
    """
    if len(scopes) == 0:
        raise StoreError("scopes must not be empty")

    stmt = (
        pg_insert(AgentGoogleBinding)
        .values(
            agent_id=agent_id,
            email=email,
            scopes=list(scopes),
        )
        .on_conflict_do_update(
            constraint="agent_google_binding_pkey",
            set_={
                "email": email,
                "scopes": list(scopes),
                "updated_at": func.now(),
            },
        )
        .returning(AgentGoogleBinding)
    )
    result = await session.execute(stmt)
    orm = result.scalar_one()
    await session.flush()
    return AgentGoogleBindingRow.model_validate(orm)
