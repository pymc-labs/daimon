"""Compatible queue readers ignore future reasons without hiding known work."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest
from daimon.core._models import TaskContinuation
from daimon.core.stores import task_continuations as queue
from daimon.core.stores.domain import ContinuationReason
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import make_account, make_tenant
from pydantic import ValidationError
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.fresh_schema
NOW = datetime(2026, 10, 13, tzinfo=UTC)
LEASE = timedelta(minutes=1)


@pytest.fixture
async def address(db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]):
    require_owned_sessionmaker(db_session_factory)
    # Only this test's disposable, shared-fixture-owned schema is widened.
    await db_session.execute(
        text("ALTER TABLE task_continuations DROP CONSTRAINT ck_task_continuations_reason")
    )
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    return {"tenant_id": tenant.id, "requester_account_id": account.id}


async def seed(session, address, *, reason="timer", thread="thread", **changes):
    key = uuid.uuid4()
    await session.execute(
        insert(TaskContinuation).values(
            **address,
            platform="discord",
            parent_channel_id="channel",
            thread_id=thread,
            requester_external_user_id="human",
            target_ma_agent_id="agent",
            target_name="analyst",
            requested_work="Continue the saved work.",
            reason=reason,
            idempotency_key=key,
            **({"available_at": NOW, "created_at": NOW, "status": "pending"} | changes),
        )
    )
    return key


@pytest.mark.parametrize("future_thread", ["thread", "other-thread"])
async def test_future_reason_cannot_hide_timer_before_distinct_or_limit(
    db_session, address, future_thread
):
    for i in range(3):
        await seed(
            db_session,
            address,
            reason="future_reason",
            thread=future_thread,
            available_at=NOW - timedelta(days=1),
            created_at=NOW - timedelta(days=1, seconds=i),
        )
    key = await seed(db_session, address)
    thread = {"tenant_id": address["tenant_id"], "platform": "discord", "thread_id": "thread"}
    pending = await queue.list_pending_continuations(db_session, **thread)
    dispatchable = await queue.list_dispatchable_continuations(
        db_session, **thread, now=NOW, max_attempts=5
    )
    due = await queue.list_due_wake_rows(
        db_session, platform="discord", now=NOW, max_attempts=5, limit=1
    )
    assert [[row.idempotency_key for row in rows] for rows in (pending, dispatchable, due)] == [
        [key],
        [key],
        [key],
    ]
    requesters = await queue.list_waiting_requesters(
        db_session, tenant_id=address["tenant_id"], target_names=["analyst"]
    )
    assert len(requesters) == 1 and requesters[0].platform_user_id == "human"
    await db_session.execute(text("DELETE FROM task_continuations WHERE reason='timer'"))
    assert (
        await queue.list_waiting_requesters(
            db_session, tenant_id=address["tenant_id"], target_names=["analyst"]
        )
        == []
    )


@pytest.mark.parametrize("claimed", [False, True])
async def test_future_reason_cannot_be_read_claimed_or_mutated(db_session, address, claimed):
    key = await seed(
        db_session,
        address,
        reason="future_reason",
        status="claimed" if claimed else "pending",
        lease_owner="old-reader" if claimed else None,
        lease_expires_at=NOW - LEASE if claimed else None,
        attempts=1 if claimed else 0,
    )
    before = (await db_session.execute(select(TaskContinuation.__table__))).mappings().one()
    assert await queue.get_continuation(db_session, idempotency_key=key) is None
    assert not await queue.claim_continuation(db_session, idempotency_key=key, now=NOW)
    assert (
        await queue.claim_wake_row(
            db_session,
            idempotency_key=key,
            owner="new-reader",
            now=NOW,
            lease=LEASE,
            max_attempts=5,
        )
        is None
    )
    assert not await queue.start_wake_row(
        db_session, idempotency_key=key, owner="old-reader", now=NOW, lease=LEASE
    )
    assert not await queue.settle_wake_row(
        db_session, idempotency_key=key, owner="old-reader", status="delivered", now=NOW
    )
    assert not await queue.release_wake_row(
        db_session, idempotency_key=key, owner="old-reader", retry_at=NOW + LEASE
    )
    assert not await queue.cancel_wake_row(
        db_session, tenant_id=address["tenant_id"], idempotency_key=key
    )
    await queue.settle_continuation(db_session, idempotency_key=key, status="skipped", now=NOW)
    assert not await queue.defer_wake_rows(
        db_session,
        tenant_id=address["tenant_id"],
        platform="discord",
        thread_id="thread",
        now=NOW,
        retry_at=NOW + LEASE,
        max_attempts=5,
    )
    after = (await db_session.execute(select(TaskContinuation.__table__))).mappings().one()
    assert dict(before) == dict(after)


@pytest.mark.parametrize("started", [False, True])
async def test_cleanup_only_settles_known_expired_claims(db_session, address, started):
    state = {
        "status": "claimed",
        "lease_owner": "crashed",
        "lease_expires_at": NOW - LEASE,
        "started_at": NOW - LEASE if started else None,
        "attempts": 5,
    }
    unknown = await seed(db_session, address, reason="future_reason", **state)
    timer = await seed(db_session, address, **state)
    rows = await queue.abandon_interrupted_wake_rows(
        db_session, platform="discord", now=NOW, max_attempts=5
    )
    assert [row.idempotency_key for row in rows] == [timer]
    assert rows[0].skip_reason == ("interrupted" if started else "attempts_exhausted")
    actual = (
        (
            await db_session.execute(
                select(TaskContinuation.__table__).where(
                    TaskContinuation.idempotency_key == unknown
                )
            )
        )
        .mappings()
        .one()
    )
    assert actual["status"] == "claimed" and actual["lease_owner"] == "crashed"
    assert actual["lease_expires_at"] == state["lease_expires_at"]


@pytest.mark.parametrize("reason", get_args(ContinuationReason))
async def test_every_known_reason_keeps_original_claim_start_settle(db_session, address, reason):
    key = await seed(db_session, address, reason=reason)
    row = await queue.claim_wake_row(
        db_session, idempotency_key=key, owner="reader", now=NOW, lease=LEASE, max_attempts=5
    )
    assert row is not None and row.reason == reason
    assert await queue.start_wake_row(
        db_session, idempotency_key=key, owner="reader", now=NOW, lease=LEASE
    )
    assert await queue.settle_wake_row(
        db_session, idempotency_key=key, owner="reader", status="delivered", now=NOW
    )
    row = await queue.get_continuation(db_session, idempotency_key=key)
    assert row is not None and row.status == "delivered" and row.delivered_at == NOW


@pytest.mark.parametrize("read", ["get", "pending", "dispatchable", "due", "claim"])
async def test_known_reason_corruption_remains_a_validation_failure(db_session, address, read):
    key = await seed(db_session, address)
    await db_session.execute(
        text("ALTER TABLE task_continuations ALTER COLUMN target_name DROP NOT NULL")
    )
    await db_session.execute(text("UPDATE task_continuations SET target_name=NULL"))
    thread = {"tenant_id": address["tenant_id"], "platform": "discord", "thread_id": "thread"}
    with pytest.raises(ValidationError, match="target_name"):
        if read == "get":
            await queue.get_continuation(db_session, idempotency_key=key)
        elif read == "pending":
            await queue.list_pending_continuations(db_session, **thread)
        elif read == "dispatchable":
            await queue.list_dispatchable_continuations(
                db_session, **thread, now=NOW, max_attempts=5
            )
        elif read == "due":
            await queue.list_due_wake_rows(
                db_session, platform="discord", now=NOW, max_attempts=5, limit=1
            )
        else:
            await queue.claim_wake_row(
                db_session,
                idempotency_key=key,
                owner="reader",
                now=NOW,
                lease=LEASE,
                max_attempts=5,
            )
