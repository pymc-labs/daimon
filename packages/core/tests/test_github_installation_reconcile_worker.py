"""Worker retry bounds and the never-refreshed backfill for installation refreshes."""

from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from daimon.core._models import GitHubAppInstallation, GitHubInstallationReconciliation
from daimon.core.config import GithubAppSettings
from daimon.core.github_installation_reconcile import (
    _retry_delay,
    drain_github_installation_reconciliations,
)
from daimon.core.stores import github_app_installations as installation_store
from pydantic import SecretStr
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def test_retry_delay_stays_capped_after_many_failures() -> None:
    assert _retry_delay(10**6) == timedelta(minutes=5)


def _settings() -> GithubAppSettings:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return GithubAppSettings(app_id="123456", private_key=SecretStr(pem.decode()))


def _github(
    installation_id: int,
    repositories: list[str],
    *,
    fail: bool = False,
    missing: bool = False,
    account_id: int | None = None,
    account_login: str = "owner",
    repository_selection: str | None = "selected",
    suspended_at: str | None = None,
) -> httpx.AsyncClient:
    """Fake GitHub: the installation lookup, listing token and repository page."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(503, json={"message": "try again"})
        if request.url.path == f"/app/installations/{installation_id}":
            if missing:
                return httpx.Response(404)
            installation: dict[str, object] = {
                "id": installation_id,
                "account": {
                    "id": account_id or installation_id,
                    "login": account_login,
                    "type": "Organization",
                },
                "suspended_at": suspended_at,
            }
            if repository_selection is not None:
                installation["repository_selection"] = repository_selection
            return httpx.Response(200, json=installation)
        if request.url.path == f"/app/installations/{installation_id}/access_tokens":
            return httpx.Response(201, json={"token": "metadata-only-token"})
        if request.url.path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "total_count": len(repositories),
                    "repositories": [{"full_name": name} for name in repositories],
                },
            )
        raise AssertionError(f"unexpected GitHub API request: {request.method} {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _add_installation(
    sessionmaker: async_sessionmaker[AsyncSession],
    installation_id: int,
    *,
    repository_selection: Literal["all", "selected"] = "selected",
) -> None:
    async with sessionmaker.begin() as session:
        await installation_store.upsert_github_app(
            session,
            installation_id=installation_id,
            account_id=installation_id,
            account_login="owner",
            account_type="Organization",
            repository_selection=repository_selection,
            suspended_at=None,
        )


async def _queue_state(
    sessionmaker: async_sessionmaker[AsyncSession], installation_id: int
) -> GitHubInstallationReconciliation | None:
    async with sessionmaker() as session:
        return await session.get(
            GitHubInstallationReconciliation,
            installation_id,
            populate_existing=True,
        )


async def test_drain_backfills_snapshot_after_old_scheduler_consumed_migration_job(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Staging 2026-10-17: the previous image marked the migration's job done unwritten."""
    installation_id = 9301
    await _add_installation(db_session_factory, installation_id)
    # Migration 0108 queued the refresh; the old drainer then finished it
    # without knowing the new columns, so the row is done and nothing was written.
    async with db_session_factory.begin() as session:
        session.add(
            GitHubInstallationReconciliation(
                installation_id=installation_id, generation=1, state="done", attempts=1
            )
        )
    async with db_session_factory() as session:
        row = await installation_store.get(session, installation_id=installation_id)
    assert row is not None and row.github_refreshed_at is None

    async with _github(installation_id, ["owner/one", "owner/two"]) as client:
        processed = await drain_github_installation_reconciliations(
            sessionmaker=db_session_factory, http_client=client, github_app=_settings()
        )

    assert processed == 1, "the never-refreshed installation must be enqueued and drained"
    async with db_session_factory() as session:
        row = await installation_store.get(session, installation_id=installation_id)
    assert row is not None
    assert row.github_refreshed_at is not None
    assert row.github_repo_full_names == ("owner/one", "owner/two")
    job = await _queue_state(db_session_factory, installation_id)
    assert job is not None and job.state == "done" and job.generation == 2


async def test_reconcile_corrects_stale_app_owner_and_leaves_matching_owner_unchanged(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    installation_id = 9305
    await _add_installation(db_session_factory, installation_id)
    now = datetime.now(UTC)
    async with db_session_factory.begin() as session:
        row = await session.get(GitHubAppInstallation, installation_id)
        assert row is not None
        row.account_login = "old-owner"
        row.account_id = None
        await session.flush()
        assert await installation_store.correct_github_app_owner(
            session,
            installation_id=installation_id,
            account_id=installation_id,
            account_login="owner",
        ) == ("old-owner", None)
        assert (
            await installation_store.correct_github_app_owner(
                session,
                installation_id=installation_id,
                account_id=installation_id,
                account_login="owner",
            )
            is None
        )
        row.account_login = "old-owner"
        row.account_id = None
        await session.flush()
    async with _github(installation_id, ["owner/one"]) as client:
        assert (
            await drain_github_installation_reconciliations(
                sessionmaker=db_session_factory, http_client=client, github_app=_settings(), now=now
            )
            == 1
        )
    async with db_session_factory() as session:
        row = await installation_store.get(session, installation_id=installation_id)
        assert row is not None
        assert (row.account_login, row.account_id) == ("owner", installation_id)
        assert (
            await installation_store.correct_github_app_owner(
                session,
                installation_id=installation_id,
                account_id=installation_id,
                account_login="owner",
            )
            is None
        )


async def test_reconcile_legacy_owner_uses_existing_login_path(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    installation_id = 9306
    async with db_session_factory.begin() as session:
        await installation_store.upsert(
            session,
            installation_id=installation_id,
            account_login="old-owner",
            repo_full_names=["old-owner/repo"],
        )
        await session.execute(
            update(GitHubAppInstallation)
            .where(GitHubAppInstallation.installation_id == installation_id)
            .values(github_refreshed_at=datetime.now(UTC))
        )
    now = datetime.now(UTC)
    async with db_session_factory.begin() as session:
        from daimon.core.stores import github_installation_reconciliation as reconciliation_store

        assert await reconciliation_store.enqueue(
            session,
            installation_id=installation_id,
            delivery_id="legacy-owner-refresh",
            event="installation",
            deleted=False,
            now=now,
        )
    async with _github(installation_id, ["owner/repo"]) as client:
        assert (
            await drain_github_installation_reconciliations(
                sessionmaker=db_session_factory, http_client=client, github_app=_settings(), now=now
            )
            == 1
        )
    async with db_session_factory() as session:
        row = await installation_store.get(session, installation_id=installation_id)
    assert row is not None
    assert row.app == "legacy"
    assert row.account_login == "owner"
    assert row.account_id is None


async def test_reconcile_404_keeps_app_owner_and_marks_installation_removed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    installation_id = 9307
    await _add_installation(db_session_factory, installation_id)
    async with _github(installation_id, [], missing=True) as client:
        assert (
            await drain_github_installation_reconciliations(
                sessionmaker=db_session_factory, http_client=client, github_app=_settings()
            )
            == 1
        )
    async with db_session_factory() as session:
        row = await installation_store.get(session, installation_id=installation_id)
    assert row is not None
    assert (row.account_login, row.account_id) == ("owner", installation_id)
    assert row.github_removed_at is not None


async def test_failing_github_keeps_one_backing_off_job_without_reenqueue(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    installation_id = 9302
    await _add_installation(db_session_factory, installation_id)
    now = datetime.now(UTC)
    async with _github(installation_id, [], fail=True) as client:
        for tick in range(3):
            await drain_github_installation_reconciliations(
                sessionmaker=db_session_factory,
                http_client=client,
                github_app=_settings(),
                now=now + timedelta(seconds=tick),
            )

    job = await _queue_state(db_session_factory, installation_id)
    assert job is not None and job.state == "pending"
    assert job.generation == 1, "a retrying job already counts as queued"
    assert job.attempts == 1, "later ticks inside the backoff must not claim or reset it"
    assert job.available_at > now + timedelta(seconds=3)


async def test_drain_leaves_refreshed_and_removed_installations_alone(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    refreshed_id, removed_id = 9303, 9304
    for installation_id in (refreshed_id, removed_id):
        await _add_installation(db_session_factory, installation_id)
    now = datetime.now(UTC)
    async with db_session_factory.begin() as session:
        await session.execute(
            update(GitHubAppInstallation)
            .where(GitHubAppInstallation.installation_id == refreshed_id)
            .values(github_refreshed_at=now, github_repo_full_names=[])
        )
        await session.execute(
            update(GitHubAppInstallation)
            .where(GitHubAppInstallation.installation_id == removed_id)
            .values(github_removed_at=now)
        )

    async with _github(refreshed_id, []) as client:
        processed = await drain_github_installation_reconciliations(
            sessionmaker=db_session_factory, http_client=client, github_app=_settings()
        )

    assert processed == 0
    assert await _queue_state(db_session_factory, refreshed_id) is None
    assert await _queue_state(db_session_factory, removed_id) is None


async def _enqueue_refresh(
    sessionmaker: async_sessionmaker[AsyncSession], installation_id: int, now: datetime
) -> None:
    from daimon.core.stores import github_installation_reconciliation as reconciliation_store

    async with sessionmaker.begin() as session:
        assert await reconciliation_store.enqueue(
            session,
            installation_id=installation_id,
            delivery_id=f"selection-refresh-{installation_id}",
            event="installation",
            deleted=False,
            now=now,
        )


async def _stored_selection(
    sessionmaker: async_sessionmaker[AsyncSession], installation_id: int
) -> str | None:
    async with sessionmaker() as session:
        row = await installation_store.get(session, installation_id=installation_id)
    assert row is not None
    return row.repository_selection


async def test_reconcile_refreshes_repository_selection_from_github(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An installation switched from all to selected repositories must not keep saying "all"."""
    installation_id = 9308
    await _add_installation(db_session_factory, installation_id, repository_selection="all")
    now = datetime.now(UTC)
    await _enqueue_refresh(db_session_factory, installation_id, now)

    async with _github(installation_id, ["owner/one"], repository_selection="selected") as client:
        processed = await drain_github_installation_reconciliations(
            sessionmaker=db_session_factory, http_client=client, github_app=_settings(), now=now
        )

    assert processed == 1
    assert await _stored_selection(db_session_factory, installation_id) == "selected"


async def test_reconcile_refreshes_repository_selection_while_suspended(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    installation_id = 9309
    await _add_installation(db_session_factory, installation_id, repository_selection="all")
    now = datetime.now(UTC)
    await _enqueue_refresh(db_session_factory, installation_id, now)

    async with _github(
        installation_id,
        [],
        repository_selection="selected",
        suspended_at="2026-10-01T00:00:00Z",
    ) as client:
        assert (
            await drain_github_installation_reconciliations(
                sessionmaker=db_session_factory, http_client=client, github_app=_settings(), now=now
            )
            == 1
        )

    async with db_session_factory() as session:
        row = await installation_store.get(session, installation_id=installation_id)
    assert row is not None
    assert row.suspended_at is not None
    assert row.repository_selection == "selected"


@pytest.mark.parametrize("reported", [None, "everything"])
async def test_reconcile_keeps_stored_selection_when_github_omits_or_garbles_it(
    db_session_factory: async_sessionmaker[AsyncSession], reported: str | None
) -> None:
    installation_id = 9310 if reported is None else 9311
    await _add_installation(db_session_factory, installation_id, repository_selection="all")
    now = datetime.now(UTC)
    await _enqueue_refresh(db_session_factory, installation_id, now)

    async with _github(installation_id, ["owner/one"], repository_selection=reported) as client:
        assert (
            await drain_github_installation_reconciliations(
                sessionmaker=db_session_factory, http_client=client, github_app=_settings(), now=now
            )
            == 1
        )

    assert await _stored_selection(db_session_factory, installation_id) == "all"
