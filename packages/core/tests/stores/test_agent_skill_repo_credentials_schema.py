"""Schema-drift guard for agent_skill_repo_credentials.

The composite PK must stay (tenant_id, agent_id, repo_url): dropping repo_url
would silently collapse this table back into the one-repo-per-agent shape of
`agent_repo_binding`, and enrolling a second skill repo would evict the first.
The tenant FK must cascade so a tenant teardown strands no credential rows.
"""

from __future__ import annotations

import uuid

import pytest
from daimon.core.stores.agent_skill_repo_credentials import (
    get_skill_repo_credential,
    set_skill_repo_credential,
)
from daimon.core.stores.tenants import delete_tenant
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, Inspector, inspect
from sqlalchemy.ext.asyncio import AsyncSession

_TABLE = "agent_skill_repo_credentials"


def _pk_columns(sync_conn: Connection, table: str) -> list[str]:
    inspector: Inspector = inspect(sync_conn)
    pk = inspector.get_pk_constraint(table)
    return list(pk["constrained_columns"])


def _tenant_id_fk_ondelete(sync_conn: Connection, table: str) -> str | None:
    inspector: Inspector = inspect(sync_conn)
    for fk in inspector.get_foreign_keys(table):
        if fk["referred_table"] == "tenants" and "tenant_id" in fk["constrained_columns"]:
            options = fk.get("options") or {}
            ondelete = options.get("ondelete")
            return str(ondelete) if ondelete is not None else None
    return None


def _column_nullable(sync_conn: Connection, table: str, column: str) -> bool:
    inspector: Inspector = inspect(sync_conn)
    for col in inspector.get_columns(table):
        if col["name"] == column:
            return bool(col["nullable"])
    raise AssertionError(f"column {column} not found on {table}")


async def test_agent_skill_repo_credentials_pk_is_tenant_agent_and_repo(
    db_session: AsyncSession,
) -> None:
    pk_cols = await db_session.run_sync(lambda s: _pk_columns(s.connection(), _TABLE))
    assert pk_cols == ["tenant_id", "agent_id", "repo_url"], (
        f"{_TABLE} PK drift: repo_url must be part of the key so one agent can enroll "
        f"several skill repos; got {pk_cols}"
    )


async def test_agent_skill_repo_credentials_tenant_id_fk_cascades_on_delete(
    db_session: AsyncSession,
) -> None:
    ondelete = await db_session.run_sync(lambda s: _tenant_id_fk_ondelete(s.connection(), _TABLE))
    assert ondelete == "CASCADE", f"{_TABLE}.tenant_id FK must be ON DELETE CASCADE, got {ondelete}"


@pytest.mark.parametrize("column", ["proof_kind", "proof_at", "proof_account_id"])
async def test_agent_skill_repo_credentials_proof_columns_are_nullable(
    db_session: AsyncSession, column: str
) -> None:
    nullable = await db_session.run_sync(lambda s: _column_nullable(s.connection(), _TABLE, column))
    assert nullable, f"{column} must be nullable — an enrollment may establish no proof"


async def test_deleting_a_tenant_removes_its_skill_repo_credentials(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await set_skill_repo_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="acme/skills",
        default_branch="main",
        path="",
        ma_secret_ref="secret",
        proof=None,
    )

    await delete_tenant(db_session, tenant_id=tenant.id)

    row = await get_skill_repo_credential(
        db_session, tenant_id=tenant.id, agent_id=agent_id, repo_url="acme/skills"
    )
    assert row is None, "a tenant teardown must cascade to its skill-repo credentials"
