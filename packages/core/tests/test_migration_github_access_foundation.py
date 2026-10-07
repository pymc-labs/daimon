"""The GitHub access schema can be removed and restored on a fresh database."""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import AgentGitHubGrant, TenantGitHubRepo
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def _load_migration():
    path = Path(__file__).parents[1] / "alembic/versions/0048_github_access_foundation.py"
    spec = importlib.util.spec_from_file_location("migration_github_access_foundation", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.mark.fresh_schema
async def test_github_access_migration_round_trips(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    migration = _load_migration()
    conn = await db_session.connection()

    def run(step: str) -> Callable[[Connection], None]:
        def inner(sync_conn: Connection) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(migration, step)()

        return inner

    await conn.run_sync(run("downgrade"))
    gone = await db_session.scalar(
        text("SELECT to_regclass(current_schema() || '.tenant_github_repos')")
    )
    assert gone is None
    await conn.run_sync(run("upgrade"))
    restored = await db_session.scalar(
        text("SELECT to_regclass(current_schema() || '.tenant_github_repos')")
    )
    assert restored is not None

    # Installation cache rows are deletable, so these records keep the ID
    # without a foreign key to the cache.
    db_session.add(
        TenantGitHubRepo(
            tenant_id=tenant.id,
            repo_id=101,
            owner_id=1,
            installation_id=999999,
            repo_full_name="example/repo",
            max_access="read",
            authorized_by_github_user_id=2,
            status="active",
            version=1,
        )
    )
    await db_session.flush()
    # Exercise the 0048 table itself, before 0049 adds github_user_id.
    await db_session.execute(
        text(
            "INSERT INTO github_issued_tokens "
            "(tenant_id, agent_id, session_id, installation_id, repo_ids, permissions, "
            "grant_versions, expires_at) VALUES "
            "(:tenant_id, :agent_id, 'session', 999999, ARRAY[101]::bigint[], "
            "CAST(:permissions AS jsonb), CAST(:versions AS jsonb), :expires_at)"
        ),
        {
            "tenant_id": tenant.id,
            "agent_id": uuid.uuid4(),
            "permissions": '{"contents":"read"}',
            "versions": '{"grant:101":1,"authorization:101":1}',
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        },
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                AgentGitHubGrant(
                    tenant_id=tenant.id,
                    agent_id=uuid.uuid4(),
                    repo_id=101,
                    baseline_access="write",
                    ceiling_access="read",
                    staged=True,
                    is_working_repo=False,
                    version=1,
                )
            )
            await db_session.flush()
