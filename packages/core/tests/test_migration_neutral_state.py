"""0074 neutral state: backfill, existing thread_sessions readers, down/up twice."""

import importlib.util
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.stores import mux_state
from daimon.core.stores.thread_sessions import create_thread_session, get_live_thread_session
from daimon.testing.factories import make_tenant
from mux.contracts.ids import ChannelRef, ResourceRef, ThreadRef
from mux.contracts.usage import UsageObservation
from mux.errors import ScopeViolation
from mux.state.lease import Slot
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession

T0 = datetime(2026, 1, 1, tzinfo=UTC)
ACCOUNT_A = uuid.uuid4()
ACCOUNT_B = uuid.uuid4()


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0074_neutral_state.py"
    spec = importlib.util.spec_from_file_location("neutral_state_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(session: AsyncSession, *steps: str) -> None:
    migration = _migration()

    def apply(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            for step in steps:
                getattr(migration, step)()

    await (await session.connection()).run_sync(apply)


async def _legacy_row(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    account_id: uuid.UUID | None,
    ma_session_id: str,
    created_at: datetime,
    channel_id: str | None = "chan",
    status: str = "live",
) -> uuid.UUID:
    row_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO thread_sessions (id, tenant_id, platform, thread_id, account_id,"
            " ma_session_id, ma_agent_id, channel_id, status, created_at)"
            " VALUES (:id, :tenant, 'discord', 'th', :account, :sid, 'agent_1', :channel,"
            " :status, :created)"
        ),
        {
            "id": row_id,
            "tenant": tenant_id,
            "account": account_id,
            "sid": ma_session_id,
            "channel": channel_id,
            "status": status,
            "created": created_at,
        },
    )
    return row_id


async def _link_all(session: AsyncSession) -> int:
    linked = 0
    while batch := await mux_state.link_legacy_thread_sessions(session, batch=1):
        linked += batch
    return linked


def _slot(tenant_id: uuid.UUID, account: uuid.UUID, channel: str = "chan") -> Slot:
    ref = ChannelRef(tenant_id=str(tenant_id), platform="discord", channel_id=channel)
    return Slot(thread=ThreadRef(channel=ref, thread_id="th"), account_id=str(account))


@pytest.mark.fresh_schema
async def test_backfill_binds_caller_rows_and_leaves_readers_unchanged(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await _run(db_session, "downgrade")
    first = await _legacy_row(
        db_session,
        tenant.id,
        account_id=ACCOUNT_A,
        ma_session_id="s_old",
        created_at=T0,
        status="dead",
    )
    second = await _legacy_row(
        db_session,
        tenant.id,
        account_id=ACCOUNT_A,
        ma_session_id="s_new",
        created_at=T0 + timedelta(hours=1),
    )
    other = await _legacy_row(
        db_session,
        tenant.id,
        account_id=ACCOUNT_B,
        ma_session_id="s_b",
        created_at=T0,
        channel_id=None,
    )
    frozen = await _legacy_row(
        db_session, tenant.id, account_id=None, ma_session_id="s_frozen", created_at=T0
    )
    await _run(db_session, "upgrade")

    binding = await mux_state.get_binding(db_session, _slot(tenant.id, ACCOUNT_A))
    assert binding is not None
    assert (binding.id, binding.generation) == (str(first), 2)
    assert dict(binding.native_refs) == {"session": "s_new", "agent": "agent_1"}
    assert (binding.provider, binding.profile) == ("anthropic", "anthropic.managed_agents")
    assert binding.legacy_account_id == str(ACCOUNT_A)
    no_channel = await mux_state.get_binding(db_session, _slot(tenant.id, ACCOUNT_B, ""))
    assert no_channel is not None and no_channel.id == str(other)
    # The migration leaves the new columns NULL; linking fills them in batches.
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM thread_sessions WHERE binding_id IS NOT NULL")
        )
        == 0
    )
    assert await _link_all(db_session) == 3
    assert await mux_state.link_legacy_thread_sessions(db_session) == 0
    rows = dict(
        (r.id, (r.binding_id, r.binding_generation))
        for r in await db_session.execute(
            text("SELECT id, binding_id, binding_generation FROM thread_sessions")
        )
    )
    assert rows == {
        first: (str(first), 1),
        second: (str(first), 2),
        other: (str(other), 1),
        frozen: (None, None),
    }
    owners = dict(
        (r.session_id, r.binding_id)
        for r in await db_session.execute(
            text("SELECT session_id, binding_id FROM journal_session")
        )
    )
    assert owners == {"s_old": str(first), "s_new": str(first), "s_b": str(other)}
    assert await db_session.scalar(text("SELECT count(*) FROM provider_binding_slot")) == 2

    # Existing readers and writers see the same rows; new rows carry no binding.
    live = await get_live_thread_session(
        db_session, tenant_id=tenant.id, platform="discord", thread_id="th", account_id=ACCOUNT_A
    )
    assert live is not None and live.id == second and live.ma_session_id == "s_new"
    created = await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="th2",
        account_id=ACCOUNT_A,
        ma_session_id="s_fresh",
    )
    assert (
        await db_session.scalar(
            text("SELECT binding_id FROM thread_sessions WHERE id = :id"), {"id": created.id}
        )
        is None
    )


@pytest.mark.fresh_schema
async def test_down_and_up_twice_keeps_thread_sessions(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _run(db_session, "downgrade")
    row = await _legacy_row(
        db_session, tenant.id, account_id=ACCOUNT_A, ma_session_id="s1", created_at=T0
    )
    await _run(db_session, "upgrade", "downgrade", "upgrade", "downgrade", "upgrade")
    assert await db_session.scalar(text("SELECT count(*) FROM provider_binding")) == 1
    assert await _link_all(db_session) == 1
    assert await db_session.scalar(
        text("SELECT binding_id FROM thread_sessions WHERE id = :id"), {"id": row}
    ) == str(row)
    live = await get_live_thread_session(
        db_session, tenant_id=tenant.id, platform="discord", thread_id="th", account_id=ACCOUNT_A
    )
    assert live is not None and live.id == row


@pytest.mark.fresh_schema
async def test_the_current_generation_is_the_row_the_legacy_reader_picks(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await _run(db_session, "downgrade")
    live = await _legacy_row(
        db_session, tenant.id, account_id=ACCOUNT_A, ma_session_id="s_live", created_at=T0
    )
    newer_dead = await _legacy_row(
        db_session,
        tenant.id,
        account_id=ACCOUNT_A,
        ma_session_id="s_dead",
        created_at=T0 + timedelta(hours=1),
        status="dead",
    )
    await _legacy_row(
        db_session,
        tenant.id,
        account_id=ACCOUNT_B,
        ma_session_id="s_retired",
        created_at=T0,
        status="retired",
    )
    await _run(db_session, "upgrade")

    legacy = await get_live_thread_session(
        db_session, tenant_id=tenant.id, platform="discord", thread_id="th", account_id=ACCOUNT_A
    )
    binding = await mux_state.get_binding(db_session, _slot(tenant.id, ACCOUNT_A))
    assert legacy is not None and legacy.id == live
    assert binding is not None and binding.native_refs["session"] == legacy.ma_session_id
    assert binding.generation == 2
    await _link_all(db_session)
    history = await db_session.scalar(
        text("SELECT binding_generation FROM thread_sessions WHERE id = :id"), {"id": newer_dead}
    )
    assert history == 1
    # No live row: no binding, just as the legacy reader finds nothing to resume.
    assert await mux_state.get_binding(db_session, _slot(tenant.id, ACCOUNT_B)) is None


@pytest.mark.fresh_schema
async def test_a_session_two_callers_recorded_is_owned_by_neither(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _run(db_session, "downgrade")
    await _legacy_row(
        db_session, tenant.id, account_id=ACCOUNT_A, ma_session_id="s_shared", created_at=T0
    )
    await _legacy_row(
        db_session,
        tenant.id,
        account_id=ACCOUNT_B,
        ma_session_id="s_shared",
        created_at=T0 + timedelta(minutes=1),
    )
    await _run(db_session, "upgrade")

    for account in (ACCOUNT_A, ACCOUNT_B):
        assert await mux_state.get_binding(db_session, _slot(tenant.id, account)) is None
    assert await db_session.scalar(text("SELECT count(*) FROM journal_session")) == 0
    observation = UsageObservation(
        id="obs",
        revision=1,
        session=ResourceRef(
            id="s_shared", kind="session", provider="anthropic", account_scope_id="ws"
        ),
        grain="model_request",
        basis="cumulative",
        output_tokens=1,
        completeness="measured",
        observed_at=T0,
    )
    with pytest.raises(ScopeViolation):
        await mux_state.record_usage(db_session, "anything", observation)


@pytest.mark.fresh_schema
async def test_one_slot_per_legacy_caller_thread_whatever_its_channels(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await _run(db_session, "downgrade")
    await _legacy_row(
        db_session,
        tenant.id,
        account_id=ACCOUNT_A,
        ma_session_id="s_old",
        created_at=T0,
        channel_id=None,
        status="dead",
    )
    await _legacy_row(
        db_session,
        tenant.id,
        account_id=ACCOUNT_A,
        ma_session_id="s_live",
        created_at=T0 + timedelta(hours=1),
        channel_id="c1",
    )
    await _legacy_row(
        db_session,
        tenant.id,
        account_id=ACCOUNT_A,
        ma_session_id="s_newest",
        created_at=T0 + timedelta(hours=2),
        channel_id=None,
        status="superseded",
    )
    await _run(db_session, "upgrade")
    assert await db_session.scalar(text("SELECT count(*) FROM provider_binding_slot")) == 1
    binding = await mux_state.get_binding(db_session, _slot(tenant.id, ACCOUNT_A, "c1"))
    assert binding is not None
    assert (binding.native_refs["session"], binding.generation) == ("s_live", 3)


@pytest.mark.slow
@pytest.mark.fresh_schema
async def test_the_exclusive_lock_on_thread_sessions_is_instant_at_200k_rows(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await _run(db_session, "downgrade")
    await db_session.execute(
        text(
            "INSERT INTO thread_sessions (tenant_id, platform, thread_id, account_id,"
            " ma_session_id, channel_id, status, created_at)"
            " SELECT :tenant, 'discord', 'th' || (n / 3), :account, 's' || n, 'chan',"
            " CASE WHEN n % 3 = 2 THEN 'live' ELSE 'dead' END,"
            " CAST(:t0 AS timestamptz) + n * interval '1 second'"
            " FROM generate_series(1, 200000) AS n"
        ),
        {"tenant": tenant.id, "account": ACCOUNT_A, "t0": T0},
    )
    migration = _migration()
    marks: dict[str, float] = {}
    add_columns = migration.add_columns

    def timed_add_columns() -> None:
        marks["lock"] = time.perf_counter()
        add_columns()
        marks["locked_step_done"] = time.perf_counter()

    setattr(migration, "add_columns", timed_add_columns)  # noqa: B010 - a module, not a typed object

    def apply(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            marks["start"] = time.perf_counter()
            migration.upgrade()
            marks["end"] = time.perf_counter()

    await (await db_session.connection()).run_sync(apply)
    # ACCESS EXCLUSIVE on thread_sessions is taken by the last step and held
    # until the commit that follows the migration.
    held = marks["end"] - marks["lock"]
    print(f"200k rows: upgrade {marks['end'] - marks['start']:.2f}s, lock held {held:.3f}s")
    assert held < 0.5
    assert marks["end"] - marks["locked_step_done"] < 0.05
    assert await db_session.scalar(text("SELECT count(*) FROM provider_binding")) > 0
