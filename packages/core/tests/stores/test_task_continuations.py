"""DB-backed tests for `daimon.core.stores.task_continuations`.

A continuation becomes a billed turn posted into a thread people are reading,
so the load-bearing property is at-most-once: `claim_continuation` must let
exactly one caller through no matter how many processes race it.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from daimon.core.stores.task_continuations import (
    claim_continuation,
    get_continuation,
    list_pending_continuations,
    record_continuation,
    settle_continuation,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def tenant_id(db_session: AsyncSession) -> uuid.UUID:
    tenant = await make_tenant(db_session)
    return tenant.id


async def test_a_recorded_continuation_starts_pending_and_undelivered(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    key = uuid.uuid4()

    row = await record_continuation(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        parent_channel_id="channel-1",
        thread_id="thread-1",
        requester_account_id=uuid.uuid4(),
        requester_external_user_id="discord-user-1",
        target_ma_agent_id="agent_stats",
        target_name="stats-bot",
        reason="task_handoff",
        idempotency_key=key,
        requested_work="finish the regression writeup",
    )

    assert row.status == "pending", "recording a continuation must never dispatch it"
    assert row.claimed_at is None and row.delivered_at is None, "nothing has run yet"
    assert row.target_ma_agent_id == "agent_stats", (
        "the target is stored concrete so a renamed agent cannot receive the handoff"
    )
    assert await get_continuation(db_session, idempotency_key=key) == row, (
        "a continuation must be readable back by the key its requester minted"
    )


async def test_a_continuation_with_no_requested_work_is_recordable(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    """A switch without work is still an audit row; it just never dispatches."""
    row = await record_continuation(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        parent_channel_id="C123",
        thread_id="1700000000.000100",
        requester_account_id=uuid.uuid4(),
        requester_external_user_id="U123",
        target_ma_agent_id="agent_stats",
        target_name="stats-bot",
        reason="task_handoff",
        idempotency_key=uuid.uuid4(),
    )

    assert row.requested_work is None, "a handoff may carry no work to continue"
    assert row.status == "pending", "the row still records the switch that happened"


async def test_claiming_then_settling_walks_the_status_ladder(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    key = uuid.uuid4()
    await record_continuation(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        parent_channel_id="channel-1",
        thread_id="thread-1",
        requester_account_id=uuid.uuid4(),
        requester_external_user_id="discord-user-1",
        target_ma_agent_id="agent_stats",
        target_name="stats-bot",
        reason="task_handoff",
        idempotency_key=key,
        requested_work="finish the regression writeup",
    )

    assert await claim_continuation(db_session, idempotency_key=key, now=_NOW) is True, (
        "the first claim on a pending continuation must win"
    )
    claimed = await get_continuation(db_session, idempotency_key=key)
    assert claimed is not None and claimed.claimed_at == _NOW, "a claim stamps when it happened"

    await settle_continuation(db_session, idempotency_key=key, status="delivered", now=_NOW)

    settled = await get_continuation(db_session, idempotency_key=key)
    assert settled is not None, "a settled continuation is kept"
    assert settled.status == "delivered", "delivery is recorded"
    assert settled.delivered_at == _NOW, "delivery is stamped"
    assert await claim_continuation(db_session, idempotency_key=key, now=_NOW) is False, (
        "a delivered continuation must never be claimable again"
    )


async def test_a_skipped_continuation_records_its_reason_and_never_stamps_delivery(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    key = uuid.uuid4()
    await record_continuation(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        parent_channel_id="channel-1",
        thread_id="thread-1",
        requester_account_id=uuid.uuid4(),
        requester_external_user_id="discord-user-1",
        target_ma_agent_id="agent_stats",
        target_name="stats-bot",
        reason="task_handoff",
        idempotency_key=key,
        requested_work="finish the regression writeup",
    )
    await claim_continuation(db_session, idempotency_key=key, now=_NOW)

    await settle_continuation(
        db_session,
        idempotency_key=key,
        status="skipped",
        now=_NOW,
        skip_reason="a newer user message superseded it",
    )

    skipped = await get_continuation(db_session, idempotency_key=key)
    assert skipped is not None, "a skipped continuation is kept for the audit trail"
    assert skipped.status == "skipped", "the skip is recorded"
    assert skipped.skip_reason == "a newer user message superseded it", (
        "why it was skipped is what the requester is eventually told"
    )
    assert skipped.delivered_at is None, "a skipped row must never read as though a turn ran"


async def test_pending_listing_covers_one_thread_and_drops_settled_rows(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    mine = uuid.uuid4()
    other_thread = uuid.uuid4()
    settled = uuid.uuid4()
    for key, thread_id in ((mine, "thread-1"), (other_thread, "thread-2"), (settled, "thread-1")):
        await record_continuation(
            db_session,
            tenant_id=tenant_id,
            platform="discord",
            parent_channel_id="channel-1",
            thread_id=thread_id,
            requester_account_id=uuid.uuid4(),
            requester_external_user_id="discord-user-1",
            target_ma_agent_id="agent_stats",
            target_name="stats-bot",
            reason="task_handoff",
            idempotency_key=key,
            requested_work="work",
        )
    await claim_continuation(db_session, idempotency_key=settled, now=_NOW)
    await settle_continuation(db_session, idempotency_key=settled, status="delivered", now=_NOW)

    pending = await list_pending_continuations(
        db_session, tenant_id=tenant_id, platform="discord", thread_id="thread-1"
    )

    assert [row.idempotency_key for row in pending] == [mine], (
        "only this thread's still-undispatched continuations may be listed"
    )


def _concurrency_dsn() -> str:
    """The real test DSN, for the one test that needs two real connections.

    The at-most-once guarantee rests on Postgres row locks serializing two
    *separate* connections; the shared single-connection `db_session` fixture
    cannot demonstrate it.
    """
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        pytest.skip("DAIMON_DATABASE__TEST_URL must be set for the concurrency test")
    return url


async def test_two_connections_racing_one_continuation_claim_it_exactly_once() -> None:
    """Two dispatchers, one continuation: one turn runs, never two.

    Runs against the default schema with a freshly minted key, so parallel
    pytest workers cannot collide.
    """
    dsn = _concurrency_dsn()
    engine_a = create_async_engine(dsn)
    engine_b = create_async_engine(dsn)
    factory_a = async_sessionmaker(engine_a, expire_on_commit=False)
    factory_b = async_sessionmaker(engine_b, expire_on_commit=False)
    key = uuid.uuid4()

    try:
        async with factory_a.begin() as seed:
            tenant = await make_tenant(seed)
            await record_continuation(
                seed,
                tenant_id=tenant.id,
                platform="discord",
                parent_channel_id="channel-race",
                thread_id="thread-race",
                requester_account_id=uuid.uuid4(),
                requester_external_user_id="discord-user-1",
                target_ma_agent_id="agent_stats",
                target_name="stats-bot",
                reason="task_handoff",
                idempotency_key=key,
                requested_work="finish the regression writeup",
            )

        async def claim(factory: async_sessionmaker[AsyncSession]) -> bool:
            async with factory.begin() as session:
                return await claim_continuation(session, idempotency_key=key, now=_NOW)

        first, second = await asyncio.gather(claim(factory_a), claim(factory_b))

        assert [first, second].count(True) == 1, (
            "exactly one racing dispatcher may claim a continuation; "
            f"got {[first, second].count(True)} winners"
        )

        async with factory_a() as check:
            row = await get_continuation(check, idempotency_key=key)
        assert row is not None and row.status == "claimed", "the winner's claim must be committed"
        assert row.claimed_at == _NOW, "the winning claim stamps the row"
    finally:
        await engine_a.dispose()
        await engine_b.dispose()
