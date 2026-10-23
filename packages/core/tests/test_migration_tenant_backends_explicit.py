"""The tenant backends-explicit migration: only a real admin choice stays explicit."""

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0123_tenant_backends_explicit.py"
    spec = importlib.util.spec_from_file_location("tenant_backends_explicit_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_the_mcp_token_surface() -> None:
    assert migration().down_revision == "0120_mcp_token_surface"


async def _row(session: AsyncSession, tenant_id: uuid.UUID, backends: str) -> None:
    await session.execute(
        text(
            "INSERT INTO tenant_access_policies (tenant_id, policy, enabled_backends) "
            "VALUES (:t, '{}'::jsonb, CAST(:b AS jsonb))"
        ),
        {"t": tenant_id, "b": backends},
    )


@pytest.mark.fresh_schema
async def test_backfill_keeps_only_admin_choices_explicit_and_matches_the_model(
    db_session: AsyncSession,
) -> None:
    untouched = await make_tenant(db_session)
    widened = await make_tenant(db_session)
    restricted = await make_tenant(db_session)
    for tenant, backends in (
        (untouched, '["anthropic"]'),
        (widened, '["anthropic", "openai"]'),
        (restricted, '["anthropic"]'),
    ):
        await _row(db_session, tenant.id, backends)
    await db_session.execute(
        text(
            "INSERT INTO security_audit_events (id, tenant_id, tool_name, operation, outcome, reason) "
            "VALUES (:id, :t, 'agent_backend', 'allowlist', 'allowed', 'completed')"
        ),
        {"id": uuid.uuid4(), "t": restricted.id},
    )
    connection = await db_session.connection()
    module = migration()
    table = "tenant_access_policies"

    def cycle(sync: Connection) -> dict[uuid.UUID, bool]:
        here = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            columns = {c["name"] for c in inspect(sync).get_columns(table, schema=here)}
            assert "enabled_backends_explicit" not in columns
            module.upgrade()
            columns_list = [c["name"] for c in inspect(sync).get_columns(table, schema=here)]
            assert columns_list == list(Base.metadata.tables[table].columns.keys())
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [d for d in differences if table in str(d)], differences
        rows = sync.execute(text(f"SELECT tenant_id, enabled_backends_explicit FROM {table}"))
        return {row.tenant_id: row.enabled_backends_explicit for row in rows}

    explicit = await connection.run_sync(cycle)
    assert explicit[untouched.id] is False, "the old server default follows the deployment"
    assert explicit[widened.id] is True, "a widened list was a choice"
    assert explicit[restricted.id] is True, "a recorded admin save was a choice"
