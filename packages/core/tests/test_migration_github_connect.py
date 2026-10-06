"""The GitHub connection schema can be removed and restored on a fresh database."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.fresh_schema
async def test_github_connect_migration_round_trips(db_session: AsyncSession) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0049_github_connect.py"
    spec = importlib.util.spec_from_file_location("migration_github_connect", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def run(step: str) -> Callable[[Connection], None]:
        def inner(sync_conn: Connection) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(migration, step)()

        return inner

    async def schema_state() -> tuple[object, object, bool]:
        invitation = await db_session.scalar(
            text("SELECT to_regclass(current_schema() || '.github_connect_invitations')")
        )
        flow = await db_session.scalar(
            text("SELECT to_regclass(current_schema() || '.github_connect_flows')")
        )
        user_id_column = bool(
            await db_session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'github_issued_tokens' AND column_name = 'github_user_id')"
                )
            )
        )
        return invitation, flow, user_id_column

    assert all(await schema_state())
    assert (
        await db_session.scalar(
            text("SELECT to_regclass(current_schema() || '.tenant_github_org_scopes')")
        )
        is None
    )
    await conn.run_sync(run("downgrade"))
    assert await schema_state() == (None, None, False)
    await conn.run_sync(run("upgrade"))
    assert all(await schema_state())
