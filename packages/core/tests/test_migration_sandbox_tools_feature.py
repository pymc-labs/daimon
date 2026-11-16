"""Migration 0206: routine sandbox opt-ins move into the feature system and back."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from daimon.core.stores.routines import create_routine
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

_TABLES = ("feature_values", "sandbox_pins", "channel_changes", "routines")
# public is on the test search_path and may hold stale tables, so look in ours only.
_HISTORY_TABLE = (
    "SELECT count(*) FROM information_schema.tables"
    " WHERE table_schema = current_schema() AND table_name = 'routine_sandbox_history'"
)


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0206_sandbox_tools_feature.py"
    spec = importlib.util.spec_from_file_location("sandbox_tools_feature_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_web_search_usage() -> None:
    """0206 sits on main's head; a sibling adding 0206 renumbers."""
    assert migration().down_revision == "0205_web_search_usage", "on main's head"


def _rows(sync: Connection, sql: str) -> list[tuple[object, ...]]:
    return [tuple(row) for row in sync.execute(text(sql))]


@pytest.mark.fresh_schema
async def test_opt_ins_and_history_move_to_the_routine_tier_and_back(
    db_session: AsyncSession,
) -> None:
    """A pinned opt-in becomes a routine-tier On with its pin; one without a pin is dropped."""
    tenant = await make_tenant(db_session)
    routines = [
        await create_routine(
            db_session,
            tenant_id=tenant.id,
            created_by_user_id="owner-1",
            agent_id="agent_a",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message=name,
        )  # fmt: skip
        for name in ("pinned", "unpinned")
    ]
    pinned, unpinned = (str(row.id) for row in routines)
    connection = await db_session.connection()
    module = migration()

    def cycle(sync: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            sync.execute(text(
                "UPDATE routines SET sandbox_tools_enabled = true, sandbox_tools_set_by = 'a',"
                " sandbox_tools_set_at = '2026-10-01T00:00Z', sandbox_agent_id = 'agent_a',"
                " sandbox_agent_version = 3, sandbox_environment_id = 'env_1',"
                " sandbox_environment_updated_at = '2026-10-01T00:00Z' WHERE id = :id"
            ), {"id": pinned})  # fmt: skip
            sync.execute(
                text("UPDATE routines SET sandbox_tools_enabled = true WHERE id = :id"),
                {"id": unpinned},
            )
            sync.execute(text(
                "INSERT INTO routine_sandbox_history (tenant_id, routine_id, before_enabled,"
                " after_enabled, set_by, set_at, via, agent_id, agent_version, environment_id,"
                " environment_updated_at) VALUES"
                " (:t, :r, false, true, 'a', '2026-09-01T00:00Z', 'mcp', 'agent_a', 2, 'env_1',"
                " '2026-09-01T00:00Z'),"
                " (:t, :r, true, false, 'o', '2026-09-02T00:00Z', 'edit', NULL, NULL, NULL, NULL),"
                " (:t, :r, false, true, 'a', '2026-10-01T00:00Z', 'cli', 'agent_a', 3, 'env_1',"
                " '2026-10-01T00:00Z')"
            ), {"t": tenant.id, "r": pinned})  # fmt: skip
            module.upgrade()
            assert _rows(
                sync, "SELECT tier, routine_id::text, value, surface FROM feature_values"
                " WHERE key = 'routine_sandbox_tools'",
            ) == [("routine", pinned, "on", "migration")], "only the pinned opt-in moves"  # fmt: skip
            assert _rows(
                sync, "SELECT tier, agent_id, routine_id::text, agent_version, environment_id"
                " FROM sandbox_pins",
            ) == [("routine", "agent_a", pinned, 3, "env_1")], "with its pin"  # fmt: skip
            history = _rows(
                sync, "SELECT surface, before ->> 'value', after -> 'pin' ->> 'agent_version'"
                " FROM channel_changes WHERE setting = 'routine_sandbox_tools' ORDER BY at",
            )  # fmt: skip
            assert history == [
                ("chat", None, "2"),
                ("system", "on", None),
                ("cli", None, "3"),
            ], "the history moves with its surfaces and pins"
            columns = {c["name"] for c in inspect(sync).get_columns("routines")}
            assert not {c for c in columns if c.startswith("sandbox_")}, "the columns go"
            assert _rows(sync, _HISTORY_TABLE) == [(0,)], "the history goes"
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [d for d in differences if any(t in str(d) for t in _TABLES)], differences

            sync.execute(text(
                "INSERT INTO feature_values (tenant_id, tier, agent_id, key, value, surface)"
                " VALUES (:t, 'agent', 'agent_a', 'routine_sandbox_tools', 'on', 'cli')"
            ), {"t": tenant.id})  # fmt: skip
            module.downgrade()
            assert _rows(
                sync, "SELECT id::text, sandbox_tools_enabled, sandbox_agent_version FROM routines"
                " ORDER BY trigger_message",
            ) == [(pinned, True, 3), (unpinned, False, None)], "the routine opt-in comes back"  # fmt: skip
            assert _rows(
                sync, "SELECT via, before_enabled, after_enabled FROM routine_sandbox_history"
                " ORDER BY set_at",
            ) == [
                ("mcp", False, True), ("edit", True, False), ("cli", False, True),
            ], "and its history"  # fmt: skip
            assert _rows(
                sync, "SELECT count(*) FROM feature_values WHERE key = 'routine_sandbox_tools'"
            ) == [(0,)], "an agent opt-in has no older form"
            module.upgrade()

    await connection.run_sync(cycle)
