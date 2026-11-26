"""The wake turns migration, on both sides of a rolling deploy.

The older release inserts and updates sessions without `wake_turns`; every row it
writes is valid, with an empty list, and this release reads and keeps it.
"""

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from daimon.core.stores import compute_sessions as sessions
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

# The older release names every column it maps, and `wake_turns` is not one of them.
_OLD_RELEASE_INSERT = text(
    "INSERT INTO compute_sessions (id, tenant_id, owner_account_id, owner_external_user_id,"
    " deployment_id, platform, parent_channel_id, thread_id, agent_id, agent_name,"
    " request_key, app_name, profile, image, region, cpus, memory_mb, ttl_hours,"
    " expires_at, created_at) VALUES (:id, :tenant, :account, 'user', 'd', 'discord',"
    " 'parent', 'thread', 'agent', 'a', :name, :name, 'mmm', 'image', 'us-central1', 2, 4096,"
    " 24, now() + interval '1 day', now())"
)
_OLD_RELEASE_UPDATE = text(
    "UPDATE compute_sessions SET state = 'running', extension_keys = '[\"k\"]' WHERE id = :id"
)


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0229_compute_started_by_wake.py"
    spec = importlib.util.spec_from_file_location("migration_0229", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_the_inline_wait_migration() -> None:
    assert migration().down_revision == "0228_compute_task_wait_until"


def _run(connection: Connection, step: str) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration(), step)()


def _wake_turns(connection: Connection) -> tuple[bool, str | None] | None:
    here = connection.scalar(text("SELECT current_schema()"))
    found = [
        (c["nullable"], c["default"])
        for c in inspect(connection).get_columns("compute_sessions", here)
        if c["name"] == "wake_turns"
    ]
    return found[0] if found else None


@pytest.mark.fresh_schema
async def test_upgrade_adds_an_empty_wake_list_and_downgrade_removes_it(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    params = {"tenant": tenant.id, "account": account.id}

    async def old_release_writes() -> uuid.UUID:
        session_id = uuid.uuid4()
        await sessions.scope(db_session, tenant.id)
        await db_session.execute(
            _OLD_RELEASE_INSERT, params | {"id": session_id, "name": session_id.hex}
        )
        await db_session.execute(_OLD_RELEASE_UPDATE, {"id": session_id})
        return session_id

    # --- The schema before the migration, where the older release writes a session.
    await connection.run_sync(_run, "downgrade")
    assert await connection.run_sync(_wake_turns) is None
    early = await old_release_writes()

    # --- The schema after it: NOT NULL, empty by default.
    await connection.run_sync(_run, "upgrade")
    assert await connection.run_sync(_wake_turns) == (False, "'[]'::jsonb")

    def matches_the_models(sync: Connection) -> None:
        differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
        assert not [d for d in differences if "compute_sessions" in str(d)], differences

    await connection.run_sync(matches_the_models)
    late = await old_release_writes()
    for session_id in (early, late):
        read = await sessions.get(db_session, tenant.id, session_id)
        assert read is not None and read.wake_turns == [] and read.extension_keys == ["k"]
    # This release keeps a list; the older release's next update leaves it as it was.
    read = await sessions.get(db_session, tenant.id, late)
    assert read is not None
    await sessions.save(db_session, read.model_copy(update={"wake_turns": ["ab"]}))
    await db_session.execute(_OLD_RELEASE_UPDATE, {"id": late})
    kept = await sessions.get(db_session, tenant.id, late)
    assert kept is not None and kept.wake_turns == ["ab"]

    # --- Back down: the column goes and every session stays.
    await connection.run_sync(_run, "downgrade")
    assert await connection.run_sync(_wake_turns) is None
    count = text("SELECT count(*) FROM compute_sessions WHERE tenant_id = :tenant")
    assert await db_session.scalar(count, params) == 2
