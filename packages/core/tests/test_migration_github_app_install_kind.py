"""Existing confirmed repositories become new-App installation rows."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import TenantGitHubRepo
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.fresh_schema
async def test_backfill_handles_missing_and_manually_inserted_installations(
    db_session: AsyncSession,
) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0058_github_app_install_kind.py"
    spec = importlib.util.spec_from_file_location("migration_github_app_installation_kind", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def run(step: str) -> Callable[[Connection], None]:
        def inner(sync_conn: Connection) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(migration, step)()

        return inner

    await conn.run_sync(run("downgrade"))
    tenant = await make_tenant(db_session)
    await db_session.execute(
        text(
            "INSERT INTO github_app_installations "
            "(installation_id, account_login, account_type, repo_full_names) "
            "VALUES (168467799, 'manual-org', 'Organization', ARRAY[]::text[])"
        )
    )
    for installation_id, repo_id, full_name in (
        (168467799, 101, "manual-org/one"),
        (168467799, 102, "manual-org/two"),
        (167257065, 103, "other-org/three"),
    ):
        db_session.add(
            TenantGitHubRepo(
                tenant_id=tenant.id,
                repo_id=repo_id,
                owner_id=55,
                installation_id=installation_id,
                repo_full_name=full_name,
                max_access="read",
                authorized_by_github_user_id=77,
                status="active",
                version=1,
            )
        )
    await db_session.flush()
    await conn.run_sync(run("upgrade"))
    rows = (
        await db_session.execute(
            text(
                "SELECT installation_id, app, account_login, account_type, repo_full_names "
                "FROM github_app_installations ORDER BY installation_id"
            )
        )
    ).all()
    assert [(row.installation_id, row.app) for row in rows] == [
        (167257065, "github_app"),
        (168467799, "github_app"),
    ]
    assert rows[0].account_login == "other-org"
    assert rows[0].repo_full_names == ["other-org/three"]
    assert rows[1].account_login == "manual-org"
    assert rows[1].account_type == "Organization"
    assert rows[1].repo_full_names == ["manual-org/one", "manual-org/two"]
