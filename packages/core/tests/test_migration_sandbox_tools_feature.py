"""Migration 0206: routine sandbox opt-ins move into the feature system and back."""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from daimon.core.stores.domain import FeatureValueRow, SandboxPin, stored_pin
from daimon.core.stores.routines import create_routine
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

_TABLES = ("feature_values", "channel_changes", "routines")
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
                sync, "SELECT pin ->> 'agent_id', (pin ->> 'agent_version')::int,"
                " pin ->> 'environment_id', (pin ->> 'environment_updated_at')::timestamptz"
                " FROM feature_values WHERE key = 'routine_sandbox_tools'",
            ) == [("agent_a", 3, "env_1", datetime(2026, 10, 1, tzinfo=UTC))], "with its pin"  # fmt: skip
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
                "INSERT INTO feature_values (tenant_id, tier, agent_id, key, value, pin, surface)"
                " VALUES (:t, 'agent', 'agent_a', 'routine_sandbox_tools', 'on',"
                " '{\"agent_id\": \"agent_a\", \"environment_name\": \"default\"}', 'cli')"
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


_OPTED_IN = (
    "UPDATE routines SET sandbox_tools_enabled = true, sandbox_tools_set_by = 'a',"
    " sandbox_tools_set_at = '2026-10-01T00:00Z', sandbox_agent_id = 'agent_a',"
    " sandbox_agent_version = 3, sandbox_environment_id = 'env_1',"
    " sandbox_environment_updated_at = '2026-10-01T00:00Z' WHERE id = :id"
)


@pytest.mark.fresh_schema
async def test_a_dm_routine_is_dropped_whatever_the_session_timezone(
    db_session: AsyncSession,
) -> None:
    """A DM routine's full opt-in never ran built-ins, so it is not carried over; a channel
    routine's pin keeps its instant when the session is not UTC."""
    tenant = await make_tenant(db_session)
    dm, channel = [
        await create_routine(
            db_session,
            tenant_id=tenant.id,
            created_by_user_id="1234567890",
            agent_id="agent_a",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message=name,
            **kind,
        )  # fmt: skip
        for name, kind in (
            ("dm", {"destination_kind": "dm"}),
            ("channel", {"destination_kind": "channel", "destination_id": "C1"}),
        )
    ]
    connection = await db_session.connection()
    module = migration()

    def cycle(sync: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            for row in (dm, channel):
                sync.execute(text(_OPTED_IN), {"id": str(row.id)})
            sync.execute(text("SET LOCAL TIME ZONE 'Asia/Kolkata'"))
            module.upgrade()
            moved = _rows(
                sync, "SELECT routine_id::text FROM feature_values"
                " WHERE key = 'routine_sandbox_tools'",
            )  # fmt: skip
            assert moved == [(str(channel.id),)], "the DM routine's opt-in is dropped"
            pin = _rows(
                sync, "SELECT pin ->> 'environment_updated_at' FROM feature_values"
                " WHERE key = 'routine_sandbox_tools'",
            )  # fmt: skip
            assert pin == [("2026-10-01T05:30:00+05:30",)], "the pin is written in session time"
            found = FeatureValueRow.model_validate(
                sync.execute(text(
                    "SELECT tenant_id, tier, agent_id, channel_id, thread_key, routine_id, key,"
                    " value, locked, pin, set_by, surface, set_at, revision FROM feature_values"
                    " WHERE key = 'routine_sandbox_tools'"
                )).mappings().one()
            )  # fmt: skip
            assert stored_pin(found) == SandboxPin(
                "agent_a", 3, "env_1", datetime(2026, 10, 1, tzinfo=UTC)
            ), "the same instant when read back"
            module.downgrade()
            assert _rows(
                sync, "SELECT id::text, sandbox_environment_updated_at = '2026-10-01T00:00Z'"
                " FROM routines WHERE sandbox_tools_enabled",
            ) == [(str(channel.id), True)], "restored to the same instant"  # fmt: skip
            module.upgrade()

    await connection.run_sync(cycle)


@pytest.mark.fresh_schema
async def test_the_downgrade_drops_what_has_no_older_form(db_session: AsyncSession) -> None:
    """Agent opt-ins, routine Offs and Daimon's own (`system`) changes are deleted; a routine's
    pinned On and its history come back."""
    tenant = await make_tenant(db_session)
    routine = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="owner-1",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="kept",
    )
    off = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="owner-1",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="off",
    )
    connection = await db_session.connection()
    module = migration()
    pin = json.dumps({
        "agent_id": "agent_a", "agent_version": 3, "environment_id": "env_1",
        "environment_updated_at": "2026-10-01T00:00:00+00:00",
    })  # fmt: skip

    def cycle(sync: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync)):
            sync.execute(text(
                "INSERT INTO feature_values (tenant_id, tier, routine_id, key, value, pin, surface)"
                " VALUES (:t, 'routine', :on, 'routine_sandbox_tools', 'on', CAST(:pin AS jsonb),"
                " 'cli'), (:t, 'routine', :off, 'routine_sandbox_tools', 'off', NULL, 'cli')"
            ), {"t": tenant.id, "on": routine.id, "off": off.id, "pin": pin})  # fmt: skip
            sync.execute(text(
                "INSERT INTO feature_values (tenant_id, tier, agent_id, key, value, pin, surface)"
                " VALUES (:t, 'agent', 'agent_a', 'routine_sandbox_tools', 'on',"
                " CAST(:pin AS jsonb), 'cli'),"
                " (:t, 'workspace', '', 'unattended_capabilities', 'off', NULL, 'system')"
            ), {"t": tenant.id, "pin": '{"agent_id": "agent_a", "environment_name": "d"}'})  # fmt: skip
            sync.execute(text(
                "INSERT INTO channel_changes (tenant_id, platform, channel_id, setting, before,"
                " after, surface) VALUES (:t, 'discord', '*', 'routine_sandbox_tools', NULL,"
                " CAST(:after AS jsonb), 'cli'),"
                " (:t, 'discord', '*', 'unattended_capabilities', NULL, NULL, 'system')"
            ), {"t": tenant.id, "after": '{"routine": "' + str(routine.id) + '", "value": "on"}'})  # fmt: skip
            module.downgrade()
            assert _rows(sync, "SELECT count(*) FROM feature_values") == [(0,)], (
                "agent opt-ins, routine Offs and system rows go"
            )
            assert _rows(sync, "SELECT surface FROM channel_changes") == [], (
                "the sandbox history moved to its table and system changes go"
            )
            assert _rows(
                sync, "SELECT id::text FROM routines WHERE sandbox_tools_enabled"
            ) == [(str(routine.id),)], "only the routine's pinned On comes back"  # fmt: skip
            module.upgrade()

    await connection.run_sync(cycle)
