"""0140: a fleet run can owe its card a cost update (`end_notice = 'cost_owed'`)."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0140_fleet_run_cost_owed.py"
    spec = importlib.util.spec_from_file_location("fleet_run_cost_owed_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_the_ledger_estimate_migration() -> None:
    assert migration().down_revision == "0139_tenant_ledger_estimate"


def _check(sync: Connection, here: str) -> str:
    (constraint,) = [
        c
        for c in inspect(sync).get_check_constraints("fleet_run", schema=here)
        if c["name"] == "ck_fleet_run_end_notice"
    ]
    return constraint["sqltext"]


@pytest.mark.fresh_schema
async def test_fleet_run_cost_owed_migration_down_up_matches_the_model(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    module = migration()

    def cycle(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))
        assert isinstance(here, str)
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            assert "cost_owed" not in _check(sync, here)
            module.upgrade()
            assert "cost_owed" in _check(sync, here)
            (index,) = [
                i
                for i in inspect(sync).get_indexes("fleet_run", schema=here)
                if i["name"] == "ix_fleet_run_end_notice_owed"
            ]
            assert "cost_owed" in str(index.get("dialect_options", {}))
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [d for d in differences if "fleet_run" in str(d)], differences

    await connection.run_sync(cycle)


def test_a_downgrade_drops_a_pending_cost_update_to_shown() -> None:
    source = (
        Path(__file__).parents[1] / "alembic/versions/0140_fleet_run_cost_owed.py"
    ).read_text()
    downgrade = source[source.index("def downgrade") :]
    assert "SET end_notice = 'shown' WHERE end_notice = 'cost_owed'" in downgrade
    # Before the constraint that forbids 'cost_owed' comes back.
    assert downgrade.index("'cost_owed'") < downgrade.index("create_check_constraint")
