"""0074 neutral state: backfill, existing thread_sessions readers, down/up twice."""

import importlib.util
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
from mux.contracts.ids import ChannelRef, ThreadRef
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
    assert await db_session.scalar(
        text("SELECT binding_id FROM thread_sessions WHERE id = :id"), {"id": row}
    ) == str(row)
    live = await get_live_thread_session(
        db_session, tenant_id=tenant.id, platform="discord", thread_id="th", account_id=ACCOUNT_A
    )
    assert live is not None and live.id == row
