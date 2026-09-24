"""PostgreSQL tests for coalesced installation refresh leases and delete fencing."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core.stores import github_app_installations as installation_store
from daimon.core.stores import github_installation_reconciliation as store
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_duplicate_delivery_does_not_advance_generation_or_delete_installation(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with db_session_factory.begin() as session:
        await installation_store.upsert(
            session,
            installation_id=8801,
            account_login="owner",
            repo_full_names=["owner/repo"],
        )
        first = await store.enqueue(
            session,
            installation_id=8801,
            delivery_id="install-delivery-1",
            event="installation",
            deleted=False,
            now=now,
        )
        duplicate = await store.enqueue(
            session,
            installation_id=8801,
            delivery_id="install-delivery-1",
            event="installation",
            deleted=True,
            now=now + timedelta(seconds=1),
        )
        job = await store.get(session, installation_id=8801)
        installation = await installation_store.get(session, installation_id=8801)

    assert first, "the first webhook should be durably recorded"
    assert not duplicate, "a repeated delivery ID should not enqueue another generation"
    assert job is not None and job.generation == 1, "duplicate deliveries must not advance work"
    assert installation is not None, "a duplicate receipt must not replay its delete action"


async def test_deleted_delivery_fences_a_delayed_repository_snapshot(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    stale_owner = uuid.uuid4()
    async with db_session_factory.begin() as session:
        await store.enqueue(
            session,
            installation_id=8802,
            delivery_id="install-create",
            event="installation",
            deleted=False,
            now=now,
        )
        stale_job = await store.claim_due(
            session,
            lease_owner=stale_owner,
            lease_for=timedelta(minutes=1),
            now=now,
        )
    assert stale_job is not None, "the create notification should be claimed for refresh"

    async with db_session_factory.begin() as session:
        await store.enqueue(
            session,
            installation_id=8802,
            delivery_id="install-delete",
            event="installation",
            deleted=True,
            now=now + timedelta(seconds=1),
        )
        stale_finish = await store.finish(
            session,
            job=stale_job,
            lease_owner=stale_owner,
            account_login="owner",
            repos=["owner/stale-repository"],
            now=now + timedelta(seconds=2),
        )
        current_job = await store.get(session, installation_id=8802)
        installation = await installation_store.get(session, installation_id=8802)

    assert not stale_finish, "a pre-delete API result must not write after the generation changes"
    assert installation is None, "the deletion notification should clear the cached repositories"
    assert current_job is not None and current_job.state == "pending", (
        "the current installation state should remain queued for API verification"
    )
    assert current_job.generation == 2, "the delete notification should fence the older claim"
