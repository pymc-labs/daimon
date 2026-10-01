"""Schema-drift guard for message_feedback.

The single-column PK on `id`, the tenant-scoped uniqueness key, and both FK
cascades are load-bearing invariants: a re-vote upserts on the uniqueness
key, and either a tenant or an account teardown must not strand rows.
"""

from __future__ import annotations

from sqlalchemy import Connection, Inspector, inspect
from sqlalchemy.ext.asyncio import AsyncSession


def _pk_columns(sync_conn: Connection, table: str) -> list[str]:
    inspector: Inspector = inspect(sync_conn)
    pk = inspector.get_pk_constraint(table)
    return list(pk["constrained_columns"])


def _fk_ondelete(sync_conn: Connection, table: str, column: str, referred_table: str) -> str | None:
    inspector: Inspector = inspect(sync_conn)
    for fk in inspector.get_foreign_keys(table):
        if fk["referred_table"] == referred_table and column in fk["constrained_columns"]:
            options = fk.get("options") or {}
            ondelete = options.get("ondelete")
            return str(ondelete) if ondelete is not None else None
    return None


def _unique_constraint_columns(sync_conn: Connection, table: str, name: str) -> list[str] | None:
    inspector: Inspector = inspect(sync_conn)
    for uc in inspector.get_unique_constraints(table):
        if uc["name"] == name:
            return list(uc["column_names"])
    return None


async def test_message_feedback_has_single_column_id_pk(db_session: AsyncSession) -> None:
    pk_cols = await db_session.run_sync(lambda s: _pk_columns(s.connection(), "message_feedback"))
    assert pk_cols == ["id"], f"message_feedback PK drift: expected ['id'], got {pk_cols}"


async def test_message_feedback_tenant_id_fk_cascades_on_delete(
    db_session: AsyncSession,
) -> None:
    ondelete = await db_session.run_sync(
        lambda s: _fk_ondelete(s.connection(), "message_feedback", "tenant_id", "tenants")
    )
    assert ondelete == "CASCADE", (
        f"message_feedback.tenant_id FK must be ON DELETE CASCADE, got {ondelete}"
    )


async def test_message_feedback_account_id_fk_exists_and_cascades_on_delete(
    db_session: AsyncSession,
) -> None:
    ondelete = await db_session.run_sync(
        lambda s: _fk_ondelete(s.connection(), "message_feedback", "account_id", "accounts")
    )
    assert ondelete == "CASCADE", (
        f"message_feedback.account_id FK must be ON DELETE CASCADE, got {ondelete}"
    )


async def test_message_feedback_uniqueness_key_covers_tenant_message_voter(
    db_session: AsyncSession,
) -> None:
    columns = await db_session.run_sync(
        lambda s: _unique_constraint_columns(
            s.connection(),
            "message_feedback",
            "uq_message_feedback_tenant_message_user",
        )
    )
    assert columns == ["tenant_id", "message_id", "platform_user_id"], (
        "uq_message_feedback_tenant_message_user drift: expected "
        f"['tenant_id', 'message_id', 'platform_user_id'], got {columns}"
    )
