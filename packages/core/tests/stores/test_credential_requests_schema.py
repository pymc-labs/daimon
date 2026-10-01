"""Schema-drift guard for credential_requests.

The single-column PK on `token` and the `tenant_id` FK cascade are load-bearing
invariants: a lookup is always by the opaque token, and a tenant teardown must
not strand credential-request rows.
"""

from __future__ import annotations

from sqlalchemy import Connection, Inspector, inspect
from sqlalchemy.ext.asyncio import AsyncSession


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


async def test_credential_requests_has_single_column_token_pk(db_session: AsyncSession) -> None:
    pk_cols = await db_session.run_sync(
        lambda s: _pk_columns(s.connection(), "credential_requests")
    )
    assert pk_cols == ["token"], f"credential_requests PK drift: expected ['token'], got {pk_cols}"


async def test_credential_requests_tenant_id_fk_cascades_on_delete(
    db_session: AsyncSession,
) -> None:
    ondelete = await db_session.run_sync(
        lambda s: _tenant_id_fk_ondelete(s.connection(), "credential_requests")
    )
    assert ondelete == "CASCADE", (
        f"credential_requests.tenant_id FK must be ON DELETE CASCADE, got {ondelete}"
    )
