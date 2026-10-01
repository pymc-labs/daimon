"""Schema-drift guard for agent_repo_binding's proof columns.

The three proof columns must stay nullable (a fail-closed gate depends on
NULL meaning "unproven") and the proof_account_id FK must null rather than
cascade on account deletion, so an account erasure drops attribution without
touching the tenant-owned binding row itself. The composite PK must stay
exactly (tenant_id, agent_id) — nothing in this plan changes it.
"""

from __future__ import annotations

from sqlalchemy import Connection, Inspector, inspect
from sqlalchemy.ext.asyncio import AsyncSession


def _pk_columns(sync_conn: Connection, table: str) -> list[str]:
    inspector: Inspector = inspect(sync_conn)
    pk = inspector.get_pk_constraint(table)
    return list(pk["constrained_columns"])


def _column_nullable(sync_conn: Connection, table: str, column: str) -> bool:
    inspector: Inspector = inspect(sync_conn)
    for col in inspector.get_columns(table):
        if col["name"] == column:
            return bool(col["nullable"])
    raise AssertionError(f"column {column} not found on {table}")


def _fk_ondelete(sync_conn: Connection, table: str, column: str, referred_table: str) -> str | None:
    inspector: Inspector = inspect(sync_conn)
    for fk in inspector.get_foreign_keys(table):
        if fk["referred_table"] == referred_table and column in fk["constrained_columns"]:
            options = fk.get("options") or {}
            ondelete = options.get("ondelete")
            return str(ondelete) if ondelete is not None else None
    return None


async def test_agent_repo_binding_pk_is_still_tenant_and_agent(db_session: AsyncSession) -> None:
    pk_cols = await db_session.run_sync(lambda s: _pk_columns(s.connection(), "agent_repo_binding"))
    assert pk_cols == ["tenant_id", "agent_id"], (
        f"agent_repo_binding PK drift: expected ['tenant_id', 'agent_id'], got {pk_cols}"
    )


async def test_agent_repo_binding_proof_kind_is_nullable(db_session: AsyncSession) -> None:
    nullable = await db_session.run_sync(
        lambda s: _column_nullable(s.connection(), "agent_repo_binding", "proof_kind")
    )
    assert nullable, "proof_kind must be nullable — existing rows land unproven, not backfilled"


async def test_agent_repo_binding_proof_at_is_nullable(db_session: AsyncSession) -> None:
    nullable = await db_session.run_sync(
        lambda s: _column_nullable(s.connection(), "agent_repo_binding", "proof_at")
    )
    assert nullable, "proof_at must be nullable — existing rows land unproven, not backfilled"


async def test_agent_repo_binding_proof_account_id_is_nullable(db_session: AsyncSession) -> None:
    nullable = await db_session.run_sync(
        lambda s: _column_nullable(s.connection(), "agent_repo_binding", "proof_account_id")
    )
    assert nullable, (
        "proof_account_id must be nullable — existing rows land unproven, "
        "and a system-initiated proof has no acting account"
    )


async def test_agent_repo_binding_proof_account_id_fk_nulls_on_account_delete(
    db_session: AsyncSession,
) -> None:
    ondelete = await db_session.run_sync(
        lambda s: _fk_ondelete(s.connection(), "agent_repo_binding", "proof_account_id", "accounts")
    )
    assert ondelete == "SET NULL", (
        "proof_account_id FK must be ON DELETE SET NULL — an account erasure must drop "
        f"attribution, not the tenant's binding row; got {ondelete}"
    )
