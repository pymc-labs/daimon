"""Widening wake reasons preserves existing rows across an upgrade/downgrade."""

from __future__ import annotations

import asyncio
import importlib.util
import re
import uuid
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base, TaskContinuation
from daimon.core.stores import task_continuations as queue
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import Connection, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

_LEGACY_REASONS = ("task_handoff", "private_input_applied", "timer", "github_access_ready")


def _migration():
    path = Path(__file__).parents[2] / "alembic/versions/0094_worker_finished_wakes.py"
    spec = importlib.util.spec_from_file_location("worker_finished_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0094_worker_finished_wakes"
    assert module.down_revision == "0093_fleet_runs"

    return module


@pytest.mark.fresh_schema
async def test_worker_finished_reason_upgrade_downgrade_upgrade(
    db_session: AsyncSession,
) -> None:
    module = _migration()
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    connection = await db_session.connection()
    insert = text(
        "INSERT INTO task_continuations (tenant_id, platform, parent_channel_id, "
        "thread_id, requester_account_id, requester_external_user_id, target_ma_agent_id, "
        "target_name, reason, idempotency_key) VALUES (:tenant, 'discord', 'channel', "
        "'thread', :account, 'human', 'agent', 'orchestrator', :reason, :key)"
    )

    def cycle(sync: Connection) -> None:
        schema = sync.scalar(text("SELECT current_schema()"))
        before = set(inspect(sync).get_table_names(schema=schema))
        with Operations.context(MigrationContext.configure(sync)):
            module.upgrade()
            assert sync.scalar(text("SHOW lock_timeout")) == "5s"
            for reason in (*_LEGACY_REASONS, "worker_finished"):
                sync.execute(
                    insert,
                    {
                        "tenant": tenant.id,
                        "account": account.id,
                        "reason": reason,
                        "key": uuid.uuid4(),
                    },
                )
            with pytest.raises(IntegrityError), sync.begin_nested():
                sync.execute(
                    insert,
                    {
                        "tenant": tenant.id,
                        "account": account.id,
                        "reason": "unknown",
                        "key": uuid.uuid4(),
                    },
                )
            sync.execute(
                text(
                    "UPDATE task_continuations SET status = 'cancelled' "
                    "WHERE reason = 'worker_finished'"
                )
            )
            module.downgrade()
            assert tuple(
                sync.scalars(text("SELECT reason FROM task_continuations ORDER BY reason"))
            ) == tuple(sorted(_LEGACY_REASONS))
            with pytest.raises(IntegrityError), sync.begin_nested():
                sync.execute(
                    insert,
                    {
                        "tenant": tenant.id,
                        "account": account.id,
                        "reason": "worker_finished",
                        "key": uuid.uuid4(),
                    },
                )
            module.upgrade()
            sync.execute(
                insert,
                {
                    "tenant": tenant.id,
                    "account": account.id,
                    "reason": "worker_finished",
                    "key": uuid.uuid4(),
                },
            )
            assert tuple(
                sync.scalars(text("SELECT reason FROM task_continuations ORDER BY reason"))
            ) == tuple(sorted((*_LEGACY_REASONS, "worker_finished")))
            assert set(inspect(sync).get_table_names(schema=schema)) == before
            checks = inspect(sync).get_check_constraints("task_continuations", schema=schema)
            reason_check = next(
                check["sqltext"]
                for check in checks
                if check["name"] == "ck_task_continuations_reason"
            )
            assert "worker_finished" in reason_check
            model_check = next(
                constraint
                for constraint in Base.metadata.tables["task_continuations"].constraints
                if constraint.name == "ck_task_continuations_reason"
            )
            assert re.findall(r"'([^']+)'", reason_check) == re.findall(
                r"'([^']+)'", str(model_check.sqltext)
            )
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [diff for diff in differences if "task_continuations" in str(diff)]

    await connection.run_sync(cycle)


@pytest.mark.fresh_schema
@pytest.mark.parametrize("state", ["pending", "claimed", "started"])
async def test_downgrade_requires_retirement_and_fences_stale_claim(db_session, state):
    from datetime import timedelta

    from .test_compatible_readers import NOW, seed

    module = _migration()
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    address = dict(tenant_id=tenant.id, requester_account_id=account.id)
    worker = await seed(db_session, address, reason="worker_finished")
    timer = await seed(db_session, address, thread="legacy-thread")
    if state != "pending":
        assert (
            await queue.claim_wake_row(
                db_session,
                idempotency_key=worker,
                owner="dispatcher",
                now=NOW,
                lease=timedelta(minutes=1),
                max_attempts=5,
            )
            is not None
        )
    if state == "started":
        assert await queue.start_wake_row(
            db_session,
            idempotency_key=worker,
            owner="dispatcher",
            now=NOW,
            lease=timedelta(minutes=1),
        )

    async def facts():
        return (
            await db_session.execute(
                select(TaskContinuation.__table__).order_by(TaskContinuation.id)
            )
        ).all()

    before = await facts()
    connection = await db_session.connection()

    def attempt(sync):
        with (
            Operations.context(MigrationContext.configure(sync)),
            pytest.raises(RuntimeError, match="Retire active worker-finished"),
            sync.begin_nested(),
        ):
            module.downgrade()

    await connection.run_sync(attempt)
    assert await facts() == before
    if state == "pending":
        assert await queue.cancel_wake_row(db_session, tenant_id=tenant.id, idempotency_key=worker)
    else:
        assert await queue.settle_wake_row(
            db_session,
            idempotency_key=worker,
            owner="dispatcher",
            status="skipped",
            skip_reason="drained",
            now=NOW,
        )
    legacy = await queue.get_continuation(db_session, idempotency_key=timer)

    def finish(sync):
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()

    await connection.run_sync(finish)
    assert await queue.get_continuation(db_session, idempotency_key=worker) is None
    assert await queue.get_continuation(db_session, idempotency_key=timer) == legacy
    assert not await queue.start_wake_row(
        db_session, idempotency_key=worker, owner="dispatcher", now=NOW, lease=timedelta(minutes=1)
    )
    assert not await queue.release_wake_row(
        db_session, idempotency_key=worker, owner="dispatcher", retry_at=NOW
    )
    assert not await queue.settle_wake_row(
        db_session, idempotency_key=worker, owner="dispatcher", status="delivered", now=NOW
    )


@pytest.mark.fresh_schema
async def test_downgrade_takes_bounded_exclusive_lock_before_retirement(db_session):
    module = _migration()
    connection = await db_session.connection()
    engine = connection.engine
    require_owned_sessionmaker(engine)
    # The migration must override this longer timeout with its own bound.
    await connection.execute(text("SET LOCAL lock_timeout = '1min'"))
    async with engine.connect() as writer, writer.begin():
        await writer.execute(text("LOCK TABLE task_continuations IN ROW EXCLUSIVE MODE"))

        def blocked(sync):
            with (
                Operations.context(MigrationContext.configure(sync)),
                pytest.raises(DBAPIError) as failure,
                sync.begin_nested(),
            ):
                module.downgrade()
            return failure.value

        failure = await asyncio.wait_for(connection.run_sync(blocked), timeout=30)
        assert failure.orig.sqlstate == "55P03"
        assert failure.statement == "LOCK TABLE task_continuations IN ACCESS EXCLUSIVE MODE"
        # Refusal leaves the reason check intact and the session usable.
        assert "worker_finished" in await connection.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'task_continuations'::regclass "
                "AND conname = 'ck_task_continuations_reason'"
            )
        )
