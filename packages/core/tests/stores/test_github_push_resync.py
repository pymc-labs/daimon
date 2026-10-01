"""PostgreSQL tests for durable push resync enqueue and lease transitions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core.stores import github_push_resync as store
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_enqueue_deduplicates_delivery_and_coalesces_new_push(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        first = await store.enqueue(
            session,
            repo_full_name="owner/repo",
            ref="refs/heads/main",
            delivery_id="delivery-1",
        )
        duplicate = await store.enqueue(
            session,
            repo_full_name="owner/repo",
            ref="refs/heads/main",
            delivery_id="delivery-1",
        )
        second = await store.enqueue(
            session,
            repo_full_name="owner/repo",
            ref="refs/heads/main",
            delivery_id="delivery-2",
        )
        row = await store.get_for_repo_ref(
            session, repo_full_name="owner/repo", ref="refs/heads/main"
        )

    assert first, "the first signed delivery should create durable work"
    assert not duplicate, "a repeated GitHub delivery ID should be idempotent"
    assert second, "a distinct push should advance the coalesced generation"
    assert row is not None, "the queued repository/ref row should persist"
    assert row.generation == 2, "two distinct deliveries should produce generation two"
    assert row.delivery_id == "delivery-2", "the coalesced row should retain the latest delivery"
    assert row.state == "pending", "new work should be claimable"


async def test_stale_owner_cannot_ack_and_new_generation_remains_runnable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    old_owner = uuid.uuid4()
    new_owner = uuid.uuid4()
    async with db_session_factory.begin() as session:
        await store.enqueue(
            session,
            repo_full_name="owner/repo",
            ref="refs/heads/main",
            delivery_id="delivery-1",
        )
        now = datetime.now(UTC) + timedelta(seconds=1)
        old_job = await store.claim_due(
            session,
            lease_owner=old_owner,
            lease_for=timedelta(seconds=1),
            now=now,
        )
        assert old_job is not None, "the first worker should claim the pending generation"
        await store.enqueue(
            session,
            repo_full_name="owner/repo",
            ref="refs/heads/main",
            delivery_id="delivery-2",
        )
        expired_job = await store.claim_due(
            session,
            lease_owner=new_owner,
            lease_for=timedelta(minutes=1),
            now=now + timedelta(seconds=2),
        )

    assert expired_job is not None, "an expired lease should be recoverable by a new worker"
    assert expired_job.claimed_generation == 2, "recovery should claim the newest generation"

    async with db_session_factory.begin() as session:
        old_completion = await store.complete(
            session,
            job=old_job,
            lease_owner=old_owner,
            now=now + timedelta(seconds=3),
        )
        bumped = await store.request_current_generation_pass(
            session,
            job_id=expired_job.id,
            now=now + timedelta(seconds=3),
        )
        new_completion = await store.complete(
            session,
            job=expired_job,
            lease_owner=new_owner,
            now=now + timedelta(seconds=4),
        )
        pending = await store.get_for_repo_ref(
            session, repo_full_name="owner/repo", ref="refs/heads/main"
        )
        final_job = await store.claim_due(
            session,
            lease_owner=uuid.uuid4(),
            lease_for=timedelta(minutes=1),
            now=now + timedelta(seconds=5),
        )

    assert not old_completion, "a stale lease owner must not acknowledge another claim"
    assert bumped, "a stale worker returning from an external call should request convergence"
    assert new_completion, "the current lease owner should release its claim"
    assert pending is not None, "the current repository/ref queue row should remain available"
    assert pending.state == "pending", "the convergence generation should stay queued"
    assert pending.generation == 3, "stale completion should force one extra current-state pass"
    assert final_job is not None, "the generation created by stale completion must be runnable"
    assert final_job.claimed_generation == 3, "the next worker must claim the bumped generation"


async def test_failed_job_keeps_error_and_retries_after_backoff(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = uuid.uuid4()
    async with db_session_factory.begin() as session:
        await store.enqueue(
            session,
            repo_full_name="owner/repo",
            ref="refs/heads/main",
            delivery_id="delivery-1",
        )
        now = datetime.now(UTC) + timedelta(seconds=1)
        retry_at = now + timedelta(seconds=10)
        job = await store.claim_due(
            session,
            lease_owner=owner,
            lease_for=timedelta(minutes=1),
            now=now,
        )
        assert job is not None, "the pending job should be claimable"
        released = await store.retry(
            session,
            job=job,
            lease_owner=owner,
            retry_after=retry_at,
            error="one binding sync failed",
            now=now,
        )
        early = await store.claim_due(
            session,
            lease_owner=uuid.uuid4(),
            lease_for=timedelta(minutes=1),
            now=now + timedelta(seconds=9),
        )
        row = await store.get_for_repo_ref(
            session, repo_full_name="owner/repo", ref="refs/heads/main"
        )
        later = await store.claim_due(
            session,
            lease_owner=uuid.uuid4(),
            lease_for=timedelta(minutes=1),
            now=retry_at,
        )

    assert released, "the owning worker should persist the retry transition"
    assert early is None, "backoff should keep a failing job from spinning each scheduler tick"
    assert row is not None and row.last_error == "one binding sync failed", (
        "the latest failure should remain observable while waiting for retry"
    )
    assert later is not None, "the failed job should become claimable at its retry time"
    assert later.attempts == 2, "each lease claim should increment the attempt count"


async def test_new_delivery_resets_retry_streak(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = uuid.uuid4()
    async with db_session_factory.begin() as session:
        await store.enqueue(
            session,
            repo_full_name="owner/repo",
            ref="refs/heads/main",
            delivery_id="delivery-1",
        )
        now = datetime.now(UTC) + timedelta(seconds=1)
        job = await store.claim_due(
            session,
            lease_owner=owner,
            lease_for=timedelta(minutes=1),
            now=now,
        )
        assert job is not None, "the first delivery should be claimable"
        await store.retry(
            session,
            job=job,
            lease_owner=owner,
            retry_after=now + timedelta(minutes=1),
            error="temporary failure",
            now=now,
        )
        await store.enqueue(
            session,
            repo_full_name="owner/repo",
            ref="refs/heads/main",
            delivery_id="delivery-2",
        )
        fresh = await store.claim_due(
            session,
            lease_owner=uuid.uuid4(),
            lease_for=timedelta(minutes=1),
            now=now + timedelta(seconds=1),
        )

    assert fresh is not None, "a new push should be due immediately despite old backoff"
    assert fresh.attempts == 1, "the latest delivery should start a new retry streak"
