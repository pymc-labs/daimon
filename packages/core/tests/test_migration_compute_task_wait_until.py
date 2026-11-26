"""The inline command wait migration, on both sides of a rolling deploy.

The older release writes tasks without `wait_until`; this release reads them as tasks
the scheduler polls at once.
"""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from daimon.core.compute_models import ComputeSession, ComputeTask
from daimon.core.compute_provider import app_name
from daimon.core.stores import compute_sessions as sessions
from daimon.core.stores import compute_tasks as tasks
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

# The older release names every column it maps, and `wait_until` is not one of them.
_OLD_RELEASE_INSERT = text(
    "INSERT INTO compute_tasks (id, tenant_id, session_id, owner_account_id, request_key,"
    " command_preview, timeout_minutes, deadline_at, output_path, created_at) VALUES (:id,"
    " :tenant, :session, :account, :key, 'make', 60, now() + interval '1 hour', '/tmp/x.out',"
    " now())"
)


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0228_compute_task_wait_until.py"
    spec = importlib.util.spec_from_file_location("migration_0228", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_the_own_dm_reach_migration() -> None:
    assert migration().down_revision == "0227_turn_origin_own_dm_reach"


def _run(connection: Connection, step: str) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration(), step)()


def _wait_until(connection: Connection) -> tuple[bool, str | None] | None:
    here = connection.scalar(text("SELECT current_schema()"))
    found = [
        (c["nullable"], c["default"])
        for c in inspect(connection).get_columns("compute_tasks", here)
        if c["name"] == "wait_until"
    ]
    return found[0] if found else None


@pytest.mark.fresh_schema
async def test_upgrade_adds_a_nullable_wait_and_downgrade_removes_it(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    now = datetime.now(UTC)
    session_id = uuid.uuid4()
    session = await sessions.register(
        db_session,
        ComputeSession(
            id=session_id,
            tenant_id=tenant.id,
            owner_account_id=account.id,
            owner_external_user_id="user",
            deployment_id="d",
            platform="discord",
            parent_channel_id="parent",
            thread_id="thread",
            agent_id="agent",
            agent_name="a",
            request_key=str(session_id),
            app_name=app_name("d", session_id),
            profile="mmm",
            image="image",
            region="us-central1",
            cpus=2,
            memory_mb=4096,
            ttl_hours=24,
            expires_at=now + timedelta(days=1),
            created_at=now,
        ),
        cap=9,
    )

    async def old_release_writes(key: str) -> uuid.UUID:
        task_id = uuid.uuid4()
        await sessions.scope(db_session, tenant.id)
        values = {"tenant": tenant.id, "session": session.id, "account": account.id}
        await db_session.execute(_OLD_RELEASE_INSERT, values | {"id": task_id, "key": key})
        return task_id

    # --- The schema before the migration, where the older release writes a task.
    await connection.run_sync(_run, "downgrade")
    assert await connection.run_sync(_wait_until) is None
    early = await old_release_writes("early")

    # --- The schema after it.
    await connection.run_sync(_run, "upgrade")
    assert await connection.run_sync(_wait_until) == (True, None)

    def matches_the_models(sync: Connection) -> None:
        differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
        assert not [d for d in differences if "compute_tasks" in str(d)], differences

    await connection.run_sync(matches_the_models)
    # The older release keeps writing; nobody waits for its tasks.
    late = await old_release_writes("late")
    # This release starts a task its caller waits for.
    waited = ComputeTask(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        session_id=session.id,
        owner_account_id=account.id,
        request_key="inline:1",
        command_preview="make",
        timeout_minutes=60,
        deadline_at=now + timedelta(hours=1),
        output_path="/tmp/y.out",
        created_at=now,
        wait_until=now + timedelta(minutes=1),
    )
    await tasks.register(db_session, waited, cap=9)
    read = await tasks.get(db_session, tenant.id, early)
    assert read is not None and read.wait_until is None
    polled = await tasks.list_active(db_session, tenant.id, now=now)
    assert {task.id for task in polled} == {early, late}, "the scheduler leaves the waited one"
    polled = await tasks.list_active(db_session, tenant.id, now=now + timedelta(minutes=1))
    assert {task.id for task in polled} == {early, late, waited.id}

    # --- Back down: the column goes and every task stays.
    await connection.run_sync(_run, "downgrade")
    assert await connection.run_sync(_wait_until) is None
    count = text("SELECT count(*) FROM compute_tasks WHERE tenant_id = :tenant")
    assert await db_session.scalar(count, {"tenant": tenant.id}) == 3
