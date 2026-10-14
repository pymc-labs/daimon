"""Export reader snapshot freshness uses the shared persisted-column owner."""

import pytest
from daimon.core._models import Journal, JournalSession, Tenant
from daimon.core.stores import UnflushedReadState, mux_state
from daimon.core.stores.fleet import FleetRunRow, FleetWorkerRow
from daimon.testing import fleet_export as exporter
from daimon.testing.fleet_export import DatabaseReadPort
from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.tests.test_fleet import _run, _worker


@pytest.fixture
async def pooled(db_engine, db_clean):
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def seed(pooled):
    run = await _run(pooled)
    return run, await _worker(pooled, run, 1)


async def test_cached_worker_matches_fresh_readonly_snapshot(pooled):
    run, worker = await seed(pooled)
    async with pooled() as cached:
        row = await cached.get(FleetWorkerRow, worker.id)
        assert row.version == worker.version
        await cached.commit()
        async with pooled.begin() as writer:
            await writer.execute(
                update(FleetWorkerRow)
                .where(FleetWorkerRow.id == worker.id)
                .values(version=worker.version + 1)
            )
        await cached.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        version = (
            await cached.execute(
                select(FleetWorkerRow.version).where(FleetWorkerRow.id == worker.id)
            )
        ).scalar_one()
        _, workers = await DatabaseReadPort(cached).run_status(
            tenant_id=run.tenant_id, run_id=run.id
        )
        assert workers[0].version == version == worker.version + 1
        assert row.version == worker.version  # No refresh or caller-state mutation.


@pytest.mark.parametrize("source", ["worker", "run"])
@pytest.mark.parametrize("mode", ["dirty", "deleted", "no_autoflush"])
async def test_unflushed_read_source_refuses_without_queries_or_writes(pooled, mode, source):
    run, worker = await seed(pooled)
    async with pooled() as session:
        row = await session.get(
            FleetWorkerRow if source == "worker" else FleetRunRow,
            worker.id if source == "worker" else run.id,
        )
        if mode == "deleted":
            await session.delete(row)
        else:
            if source == "worker":
                row.error_code = "locally-invented"
            else:
                row.version += 99
        statements = []

        def record(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        event.listen(session.sync_session.bind, "before_cursor_execute", record)
        try:

            async def read():
                with pytest.raises(UnflushedReadState):
                    await DatabaseReadPort(session).run_status(
                        tenant_id=run.tenant_id, run_id=run.id
                    )

            if mode == "no_autoflush":
                with session.no_autoflush:
                    await read()
            else:
                await read()
            assert not any(
                s.lstrip().upper().startswith(("UPDATE", "DELETE", "INSERT")) for s in statements
            )
            if mode != "deleted":
                if source == "worker":
                    assert row.error_code == "locally-invented"
                else:
                    assert row.version == run.version + 99
            else:
                assert row in session.deleted
        finally:
            event.remove(session.sync_session.bind, "before_cursor_execute", record)
            await session.rollback()


async def test_clean_readonly_snapshot_matches_database(pooled):
    run, worker = await seed(pooled)
    async with pooled() as session:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        _, workers = await DatabaseReadPort(session).run_status(
            tenant_id=run.tenant_id, run_id=run.id
        )
        assert workers[0].version == worker.version
        assert workers[0].error_code is None
        assert not session.dirty


async def test_same_transaction_core_update_ignores_cached_worker(pooled):
    run, worker = await seed(pooled)
    async with pooled() as session:
        row = await session.get(FleetWorkerRow, worker.id)
        await session.execute(
            update(FleetWorkerRow)
            .where(FleetWorkerRow.id == worker.id)
            .values(version=worker.version + 1)
            .execution_options(synchronize_session=False)
        )
        _, workers = await DatabaseReadPort(session).run_status(
            tenant_id=run.tenant_id, run_id=run.id
        )
        assert workers[0].version == worker.version + 1
        assert row.version == worker.version
        await session.rollback()


async def test_pending_new_and_reverted_assignments_remain_unflushed(pooled):
    run, worker = await seed(pooled)
    async with pooled() as session:
        row = await session.get(FleetWorkerRow, worker.id)
        row.error_code = "temporary"
        row.error_code = None
        pending = FleetWorkerRow()  # Intentionally incomplete: flushing would fail.
        session.add(pending)
        _, workers = await DatabaseReadPort(session).run_status(
            tenant_id=run.tenant_id, run_id=run.id
        )
        assert workers[0].version == worker.version
        assert workers[0].error_code is None
        assert pending in session.new
        assert row in session.dirty
        await session.rollback()


@pytest.mark.parametrize("mode", ["dirty", "deleted"])
async def test_unrelated_state_is_not_flushed_or_overwritten(pooled, mode):
    run, worker = await seed(pooled)
    async with pooled() as session:
        tenant = await session.get(Tenant, run.tenant_id)
        if mode == "deleted":
            await session.delete(tenant)
        else:
            tenant.provision_status = "local-only"
        _, workers = await DatabaseReadPort(session).run_status(
            tenant_id=run.tenant_id, run_id=run.id
        )
        assert workers[0].version == worker.version
        if mode == "deleted":
            assert tenant in session.deleted
        else:
            assert tenant.provision_status == "local-only"
            assert tenant in session.dirty
        await session.rollback()


@pytest.mark.parametrize("method", ["get_binding", "projection", "read_events"])
async def test_other_reader_edges_do_not_flush_pending_state(pooled, method):
    run, _ = await seed(pooled)
    async with pooled() as session:
        await session.begin()
        pending = FleetWorkerRow()
        session.add(pending)
        reader = DatabaseReadPort(session)
        if method == "get_binding":
            assert await reader.get_binding(exporter.root_slot(run)) is None
        elif method == "projection":
            assert await reader.projection("absent-private-session") is None
        else:
            assert await reader.read_events("absent-private-session", after=-1, limit=1) == []
        assert pending in session.new
        await session.rollback()


async def test_full_capture_and_bundle_use_snapshot_not_cached_generation(pooled, tmp_path):
    run, worker = await seed(pooled)
    async with pooled() as cached:
        row = await cached.get(FleetWorkerRow, worker.id)
        await cached.commit()
        async with pooled.begin() as writer:
            await writer.execute(
                update(FleetWorkerRow)
                .where(FleetWorkerRow.id == worker.id)
                .values(version=worker.version + 1, error_code="persisted_failure")
            )
        await cached.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        value = await exporter.capture_fleet(
            DatabaseReadPort(cached),
            tenant_id=run.tenant_id,
            run_id=run.id,
            attempt_id="synthetic-snapshot",
            source_sha="a" * 40,
        )
        exporter.publish_export(value, tmp_path / "bundle")
        actual = exporter.verify_export(tmp_path / "bundle")
        assert actual.fleet.workers[0].worker.version == worker.version + 1
        assert actual.worker_rows[0].version == worker.version + 1
        assert (
            actual.fleet.workers[0].worker.error_code
            == actual.worker_rows[0].error_code
            == "persisted_failure"
        )
        assert row.version == worker.version
        assert row.error_code is None
        assert (
            exporter.ExportManifest.model_validate_json(
                (tmp_path / "bundle" / "manifest.json").read_bytes()
            ).scenario_credit
            == 0
        )


@pytest.mark.parametrize("source", ["journal", "projection"])
@pytest.mark.parametrize("mode", ["dirty", "deleted"])
async def test_journal_source_guard_refuses_before_flush_and_preserves_state(pooled, source, mode):
    from packages.testing.tests.test_fleet_export import binding
    from packages.testing.tests.test_fleet_export import event as normalized_event

    run, _ = await seed(pooled)
    session_id = run.orchestrator_session_id
    native = binding(run.orchestrator_binding_id, exporter.root_slot(run), session_id).model_copy(
        update={"generation": 1}
    )
    original = normalized_event(session_id, 0, "persisted")
    async with pooled.begin() as writer:
        await mux_state.put_binding(writer, native, expected_generation=0)
        writer.add(
            Journal(
                session_id=session_id,
                sequence=0,
                tenant_id=run.tenant_id,
                source_key="synthetic-result",
                revision=0,
                preview=False,
                event=original.model_dump(mode="json"),
            )
        )
    async with pooled() as session:
        row = (
            await session.get(Journal, (session_id, 0))
            if source == "journal"
            else await session.get(JournalSession, session_id)
        )
        if mode == "deleted":
            await session.delete(row)
        elif source == "journal":
            row.event = {**row.event, "id": "locally-invented"}
        else:
            row.projection = {"unpersisted": "synthetic"}
        flushes = []

        def on_flush(*args):
            flushes.append(True)

        event.listen(session.sync_session, "before_flush", on_flush)
        try:
            with pytest.raises(UnflushedReadState):
                if source == "journal":
                    await DatabaseReadPort(session).read_events(session_id, after=-1, limit=1)
                else:
                    await DatabaseReadPort(session).projection(session_id)
            assert not flushes
            if mode == "deleted":
                assert row in session.deleted
            else:
                assert row in session.dirty
                if source == "journal":
                    assert row.event["id"] == "locally-invented"
                else:
                    assert row.projection == {"unpersisted": "synthetic"}
        finally:
            event.remove(session.sync_session, "before_flush", on_flush)
            await session.rollback()
