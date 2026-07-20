"""Schema-drift guard for agent_memory_store (agent memory feature).

Composite PK (tenant_id, agent_id) structurally enforces one store per
(tenant, agent) and tenant isolation — same invariant style as
test_agent_files_schema.py.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, Inspector, inspect
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


def _pk_columns(sync_conn: Connection, table: str) -> list[str]:
    inspector: Inspector = inspect(sync_conn)
    pk = inspector.get_pk_constraint(table)
    return list(pk["constrained_columns"])


async def test_agent_memory_store_has_composite_pk(db_session: AsyncSession) -> None:
    pk_cols = await db_session.run_sync(
        lambda s: _pk_columns(s.connection(), "agent_memory_store")
    )
    assert pk_cols == ["tenant_id", "agent_id"], (
        f"agent_memory_store composite PK drift: expected (tenant_id, agent_id), got {pk_cols}"
    )
