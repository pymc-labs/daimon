"""DM schema changes set a lock timeout before acquiring table locks."""

import importlib.util
from io import StringIO
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations


@pytest.mark.parametrize("name", ["0134_cold_dm_agent", "0135_routine_dm_destination"])
def test_dm_upgrade_sets_lock_timeout_before_ddl(name: str) -> None:
    path = Path(__file__).parents[1] / f"alembic/versions/{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context), context.begin_transaction():
        migration.upgrade()
    statements = output.getvalue().split(";\n")
    assert statements[1].strip() == "SET LOCAL lock_timeout = '5s'"
    assert statements[2].strip().startswith("ALTER TABLE ")
