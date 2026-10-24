"""PostgreSQL tests for coalesced installation refresh leases and delete fencing."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from daimon.core._models import GitHubAppInstallation, GitHubInstallationReconciliation
from daimon.core.stores import github_app_installations as installation_store
from daimon.core.stores import github_installation_reconciliation as store
from daimon.core.stores.github_installation_coverage import installation_covers_repo
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


async def test_github_app_refresh_intersects_confirmation_and_tracks_lifecycle(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    installation_id = 8917
    now = datetime.now(UTC)
    async with db_session_factory.begin() as session:
        await installation_store.upsert_github_app(
            session,
            installation_id=installation_id,
            account_id=55,
            account_login="owner",
            account_type="Organization",
            repository_selection="selected",
            suspended_at=None,
        )
        await session.execute(
            update(GitHubAppInstallation)
            .where(GitHubAppInstallation.installation_id == installation_id)
            .values(repo_full_names=["owner/confirmed", "owner/removed"])
        )

    async def refresh(
        generation: int,
        repos: list[str] | None,
        *,
        installed: bool = True,
        suspended_at: datetime | None = None,
    ) -> None:
        owner = uuid.uuid4()
        at = now + timedelta(seconds=generation)
        async with db_session_factory.begin() as session:
            assert await store.enqueue(
                session,
                installation_id=installation_id,
                delivery_id=f"github-app-refresh-{generation}",
                event="installation",
                deleted=False,
                now=at,
            )
        async with db_session_factory.begin() as session:
            job = await store.claim_due(
                session, lease_owner=owner, lease_for=timedelta(minutes=1), now=at
            )
            assert job is not None and job.installation_id == installation_id
            assert await store.finish(
                session,
                job=job,
                lease_owner=owner,
                account_login="owner" if installed else None,
                repos=repos,
                suspended_at=suspended_at,
                now=at,
            )

    async def covered(name: str) -> bool:
        async with db_session_factory() as session:
            return installation_covers_repo(
                await installation_store.get(session, installation_id=installation_id), name
            )

    assert await covered("owner/removed"), "unrefreshed installations retain confirmed access"
    await refresh(1, ["owner/confirmed"])
    assert not await covered("owner/removed"), "GitHub removal must remove coverage"
    await refresh(2, ["owner/confirmed", "owner/removed", "owner/unconfirmed"])
    assert await covered("owner/removed"), "a still-confirmed repo can recover"
    assert not await covered("owner/unconfirmed"), "GitHub alone cannot create tenant access"
    await refresh(3, None, suspended_at=now)
    assert not await covered("owner/confirmed"), "suspension disables coverage"
    await refresh(4, ["owner/confirmed", "owner/removed"])
    assert await covered("owner/confirmed"), "unsuspension restores confirmed coverage"
    await refresh(5, None, installed=False)
    async with db_session_factory() as session:
        row = await installation_store.get(session, installation_id=installation_id)
    assert row is not None and row.github_removed_at is not None
    assert row.repo_full_names == ("owner/confirmed", "owner/removed")
    assert not await covered("owner/confirmed"), "confirmed uninstall disables coverage"


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


async def test_legacy_deletion_does_not_remove_new_app_installation(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        await installation_store.upsert_github_app(
            session,
            installation_id=8899,
            account_id=55,
            account_login="owner",
            account_type="Organization",
            repository_selection="selected",
            suspended_at=None,
        )
        await store.enqueue(
            session,
            installation_id=8899,
            delivery_id="legacy-delete-new-app",
            event="installation",
            deleted=True,
            now=datetime.now(UTC),
        )
        installation = await installation_store.get(session, installation_id=8899)

    assert installation is not None and installation.app == "github_app"


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


async def test_delete_and_snapshot_completion_serialize_without_restoring_cache(
    db_committing_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    now = datetime.now(UTC)
    owner = uuid.uuid4()
    async with sessions.begin() as session:
        await installation_store.upsert(
            session,
            installation_id=8812,
            account_login="owner",
            repo_full_names=["owner/old"],
        )
        await store.enqueue(
            session,
            installation_id=8812,
            delivery_id="install-contention-create",
            event="installation",
            deleted=False,
            now=now,
        )
        job = await store.claim_due(
            session,
            lease_owner=owner,
            lease_for=timedelta(minutes=1),
            now=now,
        )
    assert job is not None

    start = asyncio.Event()

    async def finish_snapshot() -> bool:
        await start.wait()
        async with sessions.begin() as session:
            return await store.finish(
                session,
                job=job,
                lease_owner=owner,
                account_login="owner",
                repos=["owner/new"],
                now=now + timedelta(seconds=1),
            )

    async def deliver_delete() -> bool:
        await start.wait()
        async with sessions.begin() as session:
            return await store.enqueue(
                session,
                installation_id=8812,
                delivery_id="install-contention-delete",
                event="installation",
                deleted=True,
                now=now + timedelta(seconds=1),
            )

    finish_task = asyncio.create_task(finish_snapshot())
    delete_task = asyncio.create_task(deliver_delete())
    start.set()
    _, deleted = await asyncio.gather(finish_task, delete_task)

    async with sessions() as session:
        installation = await installation_store.get(session, installation_id=8812)
        current_job = await store.get(session, installation_id=8812)

    assert deleted, "the deletion delivery should be recorded"
    assert installation is None, "a deletion must not leave a completed snapshot in cache"
    assert current_job is not None and current_job.generation == 2
    assert current_job.state == "pending"


async def test_confirmation_and_reconcile_same_installation_complete_without_deadlock(
    db_nullpool_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    installation_id = 8813
    now = datetime.now(UTC)
    owner = uuid.uuid4()
    async with sessions.begin() as session:
        await installation_store.upsert_github_app(
            session,
            installation_id=installation_id,
            account_id=55,
            account_login="owner",
            account_type="Organization",
            repository_selection="selected",
            suspended_at=None,
        )
        assert await store.enqueue(
            session,
            installation_id=installation_id,
            delivery_id="before-confirmation",
            event="installation",
            deleted=False,
            now=now,
        )
    async with sessions.begin() as session:
        job = await store.claim_due(
            session, lease_owner=owner, lease_for=timedelta(minutes=1), now=now
        )
    assert job is not None
    installation_locked, worker_started = asyncio.Event(), asyncio.Event()

    async def confirm() -> None:
        async with sessions.begin() as session:
            await installation_store.upsert_github_app(
                session,
                installation_id=installation_id,
                account_id=55,
                account_login="owner",
                account_type="Organization",
                repository_selection="selected",
                suspended_at=None,
            )
            installation_locked.set()
            await worker_started.wait()
            await asyncio.sleep(0.05)
            assert await store.enqueue(
                session,
                installation_id=installation_id,
                delivery_id="during-confirmation",
                event="connect",
                deleted=False,
                now=now + timedelta(seconds=1),
            )

    async def reconcile() -> bool:
        await installation_locked.wait()
        async with sessions.begin() as session:
            worker_started.set()
            return await store.finish(
                session,
                job=job,
                lease_owner=owner,
                account_login="owner",
                repos=["owner/repo"],
                now=now + timedelta(seconds=2),
            )

    confirmed, finished = await asyncio.wait_for(asyncio.gather(confirm(), reconcile()), timeout=10)
    assert confirmed is None and not finished
    async with sessions() as session:
        current = await store.get(session, installation_id=installation_id)
        installation = await installation_store.get(session, installation_id=installation_id)
    assert current is not None and current.generation == 2 and current.state == "pending"
    assert installation is not None and installation.app == "github_app"


async def test_enqueue_never_refreshed_only_queues_idle_unrefreshed_live_installations(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    ids = {
        "never": 9101,  # no queue row
        "done": 9102,  # job completed by an old drainer, nothing written
        "pending": 9103,
        "running": 9104,
        "refreshed": 9105,
        "removed": 9106,
        "legacy": 9107,
    }
    async with db_session_factory.begin() as session:
        for name, installation_id in ids.items():
            if name == "legacy":
                await installation_store.upsert(
                    session,
                    installation_id=installation_id,
                    account_login="owner",
                    repo_full_names=[],
                )
            else:
                await installation_store.upsert_github_app(
                    session,
                    installation_id=installation_id,
                    account_id=installation_id,
                    account_login="owner",
                    account_type="Organization",
                    repository_selection="selected",
                    suspended_at=None,
                )
        await session.execute(
            update(GitHubAppInstallation)
            .where(GitHubAppInstallation.installation_id == ids["refreshed"])
            .values(github_refreshed_at=now)
        )
        await session.execute(
            update(GitHubAppInstallation)
            .where(GitHubAppInstallation.installation_id == ids["removed"])
            .values(github_removed_at=now)
        )
        for name, state in (("done", "done"), ("pending", "pending"), ("running", "running")):
            session.add(
                GitHubInstallationReconciliation(
                    installation_id=ids[name],
                    generation=4,
                    state=state,
                    attempts=3,
                    available_at=now + timedelta(minutes=3),
                    lease_owner=uuid.uuid4() if state == "running" else None,
                    lease_expires_at=now + timedelta(minutes=2) if state == "running" else None,
                    claimed_generation=4 if state == "running" else None,
                    last_error="boom" if state == "pending" else None,
                )
            )

    async with db_session_factory.begin() as session:
        queued = await store.enqueue_never_refreshed(session, now=now, limit=50)
    assert queued == 2, "only the row-less and the done-without-snapshot installations qualify"

    async with db_session_factory() as session:
        jobs = {
            name: await store.get(session, installation_id=installation_id)
            for name, installation_id in ids.items()
        }
    never, done = jobs["never"], jobs["done"]
    assert never is not None and never.state == "pending" and never.generation == 1
    assert never.available_at == now
    assert done is not None and done.state == "pending" and done.generation == 5
    assert done.attempts == 0 and done.available_at == now
    pending, running = jobs["pending"], jobs["running"]
    assert pending is not None and pending.generation == 4 and pending.attempts == 3
    assert pending.last_error == "boom", "a backing-off job must not be reset"
    assert running is not None and running.state == "running" and running.generation == 4
    assert running.lease_owner is not None, "a running lease must not be disturbed"
    assert jobs["refreshed"] is None and jobs["removed"] is None and jobs["legacy"] is None

    async with db_session_factory.begin() as session:
        again = await store.enqueue_never_refreshed(session, now=now, limit=50)
    assert again == 0, "the sweep is idempotent while jobs are queued"


async def test_enqueue_never_refreshed_is_bounded_by_limit(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        for installation_id in (9111, 9112, 9113):
            await installation_store.upsert_github_app(
                session,
                installation_id=installation_id,
                account_id=installation_id,
                account_login="owner",
                account_type="Organization",
                repository_selection="selected",
                suspended_at=None,
            )
    now = datetime.now(UTC)
    async with db_session_factory.begin() as session:
        assert await store.enqueue_never_refreshed(session, now=now, limit=2) == 2
    async with db_session_factory.begin() as session:
        assert await store.enqueue_never_refreshed(session, now=now, limit=2) == 1
