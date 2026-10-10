"""Additive catalog migration up/down/up, ORM equivalence and legacy isolation."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from daimon.core.stores.user_skills import upsert_user_skill
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

TABLES = {"neutral_agent_revisions", "neutral_skill_versions"}


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0079_neutral_catalog.py"
    spec = importlib.util.spec_from_file_location("neutral_catalog_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.fresh_schema
async def test_catalog_migration_upgrade_downgrade_upgrade_preserves_legacy(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await upsert_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=account.id,
        agent_name="dev",
        name="planning",
        source_repo_url="https://example.test/repo",
        source_repo_branch="main",
        source_path="planning",
        content_hash="legacy",
        anthropic_id="anthropic-skill",
        anthropic_latest_version="1",
    )
    before = (
        await db_session.execute(text("SELECT row_to_json(user_skills) FROM user_skills"))
    ).scalar_one()
    connection = await db_session.connection()
    module = migration()

    def cycle(sync: Connection) -> None:
        # The test fixture creates mapped tables; first return to pre-migration.
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            assert not TABLES.intersection(inspect(sync).get_table_names())
            module.upgrade()
            assert sync.scalar(text("SHOW lock_timeout")) == "5s"
            assert TABLES.issubset(inspect(sync).get_table_names())
            # Compare real migration DDL with all mapped columns, keys, checks and FKs.
            for name in TABLES:
                actual = inspect(sync).get_columns(name)
                assert [column["name"] for column in actual] == list(
                    Base.metadata.tables[name].columns.keys()
                )
                assert {check["name"] for check in inspect(sync).get_check_constraints(name)} == {
                    constraint.name
                    for constraint in Base.metadata.tables[name].constraints
                    if constraint.__class__.__name__ == "CheckConstraint"
                }
            context = MigrationContext.configure(sync)
            differences = compare_metadata(context, Base.metadata)
            assert not [diff for diff in differences if any(name in str(diff) for name in TABLES)]
            values = {"tenant": tenant.id, "account": account.id}
            sync.execute(
                text(
                    "INSERT INTO neutral_agent_revisions VALUES "
                    "(:tenant, :account, 'openai', 'ws', 'agent', 1, 'principal', '{}'::jsonb)"
                ),
                values,
            )
            sync.execute(
                text(
                    "INSERT INTO neutral_skill_versions VALUES "
                    "(:tenant, :account, 'openai', 'ws', 'skill', 'v1', 'principal', "
                    "'dev', 'planning', '{}'::jsonb, decode('00', 'hex'))"
                ),
                values,
            )
            module.downgrade()
            assert not TABLES.intersection(inspect(sync).get_table_names())
            module.upgrade()
            assert sync.scalar(text("SHOW lock_timeout")) == "5s"
            assert TABLES.issubset(inspect(sync).get_table_names())

    await connection.run_sync(cycle)
    after = (
        await db_session.execute(text("SELECT row_to_json(user_skills) FROM user_skills"))
    ).scalar_one()
    assert after == before, "legacy columns and rows stay byte-equivalent"
    for name in sorted(TABLES):
        assert await db_session.scalar(text(f"SELECT count(*) FROM {name}")) == 0
