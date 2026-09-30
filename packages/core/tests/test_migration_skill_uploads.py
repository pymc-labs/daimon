"""The skill uploads migration marks every existing ledger row as a repo sync."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.stores.user_skills import upsert_user_skill
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def _load_migration():
    path = Path(__file__).parents[1] / "alembic/versions/0035_skill_uploads.py"
    spec = importlib.util.spec_from_file_location("migration_skill_uploads", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.mark.fresh_schema
async def test_skill_uploads_migration_round_trips(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await upsert_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=uuid.uuid4(),
        agent_name="agent",
        name="notes",
        source_repo_url="https://github.com/o/r",
        source_repo_branch="main",
        source_path="notes",
        content_hash="h",
        anthropic_id="sk_1",
        anthropic_latest_version="1",
    )
    migration = _load_migration()
    conn = await db_session.connection()

    def run(step: str):
        def inner(sync_conn):
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(migration, step)()

        return inner

    await conn.run_sync(run("downgrade"))
    await conn.run_sync(run("upgrade"))

    row = (
        await db_session.execute(
            text("SELECT source, origin, added_by_account_id FROM user_skills")
        )
    ).one()
    assert tuple(row) == ("repo", "", None), "existing rows are repo syncs"
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(text("UPDATE user_skills SET source = 'other'"))
