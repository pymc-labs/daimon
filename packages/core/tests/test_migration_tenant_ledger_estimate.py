"""0139: `tenant_ledger.estimate` marks a debit priced from incomplete provider counts."""

import importlib.util
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0139_tenant_ledger_estimate.py"
    spec = importlib.util.spec_from_file_location("tenant_ledger_estimate_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_the_skill_bytes_migration() -> None:
    assert migration().down_revision == "0135_mcp_token_skill_bytes"


@pytest.mark.fresh_schema
async def test_tenant_ledger_estimate_migration_down_up_matches_the_model(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    module = migration()
    table = "tenant_ledger"

    def cycle(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            columns = {c["name"] for c in inspect(sync).get_columns(table, schema=here)}
            assert "estimate" not in columns
            module.upgrade()
            columns = [c["name"] for c in inspect(sync).get_columns(table, schema=here)]
            assert columns == list(Base.metadata.tables[table].columns.keys()), "column order"
            checks = {c["name"] for c in inspect(sync).get_check_constraints(table, schema=here)}
            assert "ck_tenant_ledger_estimate" in checks
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [d for d in differences if table in str(d)], differences

    await connection.run_sync(cycle)


@pytest.mark.fresh_schema
async def test_only_a_known_estimate_kind_is_stored(db_session: AsyncSession) -> None:
    from daimon.core.stores import tenant_ledger
    from daimon.testing.factories import make_tenant

    tenant = await make_tenant(db_session, id=uuid4())
    assert await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("-0.001366"),
        reason="turn_debit",
        idempotency_key="turn:s:known",
        estimate="cache_writes_unreported",
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await tenant_ledger.insert_entry(
                db_session,
                tenant_id=tenant.id,
                delta_usd=Decimal("-1"),
                reason="turn_debit",
                idempotency_key="turn:s:free-text",
                estimate="a guess",  # pyright: ignore[reportArgumentType]
            )
