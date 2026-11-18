"""Orphan marker recovery keeps an unconfirmed MA session out of reuse."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from daimon.core.stores.thread_sessions import (
    clear_active_turn_if_message_id,
    create_thread_session,
    get_live_thread_session,
    get_thread_session_by_id,
    mark_turn_active,
)
from daimon.core.turn.bookkeeping import orphan_scope, recover_orphan_marker
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize("interrupt_succeeded", [False, True])
async def test_orphan_interrupt_controls_session_reuse(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    interrupt_succeeded: bool,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="orphan-recovery",
        account_id=account.id,
        ma_session_id="sess_busy",
    )
    await mark_turn_active(
        db_session, id=row.id, active_turn_message_id="card-old", now=datetime.now(UTC)
    )
    await db_session.commit()
    row = (await get_thread_session_by_id(db_session, id=row.id)) or row
    interrupt = AsyncMock(return_value=interrupt_succeeded)

    assert await recover_orphan_marker(
        db_session_factory, row, clear=clear_active_turn_if_message_id, interrupt=interrupt
    )
    interrupt.assert_awaited_once_with(session_id="sess_busy")
    async with db_session_factory() as session:
        old = await get_thread_session_by_id(session, id=row.id)
        live = await get_live_thread_session(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="orphan-recovery",
            account_id=account.id,
        )
    assert old is not None and old.active_turn_message_id is None
    if interrupt_succeeded:
        assert live is not None and live.id == row.id
    else:
        assert old.status == "dead"
        assert live is None, "the next preparation must create a fresh MA session"


async def test_moved_orphan_marker_leaves_new_owner_alone(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="moved-orphan",
        account_id=account.id,
        ma_session_id="sess_live",
    )
    await mark_turn_active(
        db_session, id=row.id, active_turn_message_id="card-old", now=datetime.now(UTC)
    )
    await db_session.commit()
    stale = (await get_thread_session_by_id(db_session, id=row.id)) or row
    await mark_turn_active(
        db_session, id=row.id, active_turn_message_id="card-new", now=datetime.now(UTC)
    )
    await db_session.commit()
    interrupt = AsyncMock(return_value=False)

    assert not await recover_orphan_marker(
        db_session_factory, stale, clear=clear_active_turn_if_message_id, interrupt=interrupt
    )
    interrupt.assert_not_awaited()
    async with db_session_factory() as session:
        current = await get_thread_session_by_id(session, id=row.id)
    assert current is not None and current.status == "live"
    assert current.active_turn_message_id == "card-new"


async def test_failed_detach_rolls_back_marker_clear(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="rollback-orphan",
        account_id=account.id,
        ma_session_id="sess_busy",
    )
    await mark_turn_active(
        db_session, id=row.id, active_turn_message_id="card-old", now=datetime.now(UTC)
    )
    await db_session.commit()
    snapshot = (await get_thread_session_by_id(db_session, id=row.id)) or row

    async def fail_detach(_session: AsyncSession, *, id: object) -> None:
        raise RuntimeError("database detach failed")

    monkeypatch.setattr("daimon.core.turn.bookkeeping.mark_dead", fail_detach)
    with pytest.raises(RuntimeError, match="database detach failed"):
        await recover_orphan_marker(
            db_session_factory,
            snapshot,
            clear=clear_active_turn_if_message_id,
            interrupt=AsyncMock(return_value=False),
        )
    async with db_session_factory() as session:
        current = await get_thread_session_by_id(session, id=row.id)
    assert current is not None and current.status == "live"
    assert current.active_turn_message_id == "card-old", "the next sweep can retry"


@pytest.mark.parametrize("platform", ["discord", "slack", "teams", "web"])
async def test_orphan_scope_names_the_sweep_and_needs_an_account(
    db_session: AsyncSession, platform: str
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id=f"scope-{platform}",
        account_id=account.id,
        ma_session_id="sess_scope",
    )
    scope = orphan_scope(row, platform=platform)
    assert scope is not None and scope.authorization_id == f"{platform}-orphan-sweep"
    assert scope.account_id == str(account.id) and scope.principal_id == "daimon"
    assert orphan_scope(row.model_copy(update={"account_id": None}), platform=platform) is None
