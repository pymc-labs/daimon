"""Schema-drift guard for composite primary keys.

Cross-agent isolation in the MCP self-edit tools is enforced
structurally by the composite primary keys on ``agent_files`` and
``agent_repo_binding``. If a future migration drops or alters either key,
agents could read or overwrite each other's rows silently. These tests
fail loudly the moment that invariant breaks.
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


async def test_agent_files_has_composite_pk(db_session: AsyncSession) -> None:
    """SC-3 structural: PK on agent_files is (tenant_id, agent_id, key)."""
    pk_cols = await db_session.run_sync(lambda s: _pk_columns(s.connection(), "agent_files"))
    assert pk_cols == ["tenant_id", "agent_id", "key"], (
        f"agent_files composite PK drift: expected (tenant_id, agent_id, key), got {pk_cols}"
    )


async def test_agent_repo_binding_has_composite_pk(db_session: AsyncSession) -> None:
    """SC-3 structural: PK on agent_repo_binding is (tenant_id, agent_id)."""
    pk_cols = await db_session.run_sync(lambda s: _pk_columns(s.connection(), "agent_repo_binding"))
    assert pk_cols == ["tenant_id", "agent_id"], (
        f"agent_repo_binding composite PK drift: expected (tenant_id, agent_id), got {pk_cols}"
    )
