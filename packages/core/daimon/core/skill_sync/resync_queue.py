"""Scheduler shell for durable GitHub push-driven skill resyncs."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.core.config import GithubSettings
from daimon.core.skill_sync.resync import resync_bound_repo
from daimon.core.stores import github_push_resync as resync_store
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)
_LEASE_FOR = timedelta(minutes=2)
_LEASE_RENEW_INTERVAL = timedelta(seconds=30)
_MAX_JOBS_PER_TICK = 3
_MAX_RETRY_DELAY = timedelta(minutes=5)


async def drain_github_push_resync_queue(
    *,
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet | None,
    anthropic_client: AsyncAnthropic,
    github_settings: GithubSettings,
    now: datetime | None = None,
) -> int:
    """Run up to three due jobs, retaining failures for retry on later ticks."""
    jobs_processed = 0
    for _ in range(_MAX_JOBS_PER_TICK):
        current_time = now or datetime.now(UTC)
        lease_owner = uuid.uuid4()
        async with sessionmaker.begin() as session:
            job = await resync_store.claim_due(
                session,
                lease_owner=lease_owner,
                lease_for=_LEASE_FOR,
                now=current_time,
            )
        if job is None:
            break

        lock_connection = await engine.connect()
        lock_value = f"github-push-resync:{job.repo_full_name}:{job.ref}"
        try:
            result = await lock_connection.execute(
                text("SELECT pg_try_advisory_lock(hashtextextended(:lock_value, 0))"),
                {"lock_value": lock_value},
            )
            lock_acquired = result.scalar_one()
            await lock_connection.commit()
            if not lock_acquired:
                await lock_connection.close()
                await _retry_job(
                    sessionmaker=sessionmaker,
                    job=job,
                    lease_owner=lease_owner,
                    error="repository sync lock is held",
                    delay=timedelta(seconds=5),
                )
                jobs_processed += 1
                continue
        except BaseException:
            await lock_connection.close()
            await _retry_job(
                sessionmaker=sessionmaker,
                job=job,
                lease_owner=lease_owner,
                error="could not acquire repository sync lock",
                delay=timedelta(seconds=5),
            )
            raise

        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            _renew_lease(
                sessionmaker=sessionmaker,
                job_id=job.id,
                lease_owner=lease_owner,
                lease_lost=lease_lost,
            )
        )
        try:
            if fernet is None:
                raise RuntimeError("crypto keys are not configured for GitHub resync")
            report = await resync_bound_repo(
                repo_full_name=job.repo_full_name,
                ref=job.ref,
                sessionmaker=sessionmaker,
                fernet=fernet,
                anthropic_client=anthropic_client,
                github_settings=github_settings,
            )
            current_time = datetime.now(UTC)
            async with sessionmaker.begin() as session:
                if report.failed_bindings:
                    delay = _retry_delay(job.attempts)
                    released = await resync_store.retry(
                        session,
                        job=job,
                        lease_owner=lease_owner,
                        retry_after=current_time + delay,
                        error=f"{report.failed_bindings} binding sync(s) failed",
                        now=current_time,
                    )
                else:
                    released = await resync_store.complete(
                        session,
                        job=job,
                        lease_owner=lease_owner,
                        now=current_time,
                    )
                if not released:
                    await resync_store.request_current_generation_pass(
                        session,
                        job_id=job.id,
                        now=current_time,
                    )
            _log.info(
                "github.resync_queue.job_finished",
                repo=job.repo_full_name,
                ref=job.ref,
                generation=job.claimed_generation,
                failed_bindings=report.failed_bindings,
                lease_lost=lease_lost.is_set(),
            )
        except asyncio.CancelledError:
            await _retry_job(
                sessionmaker=sessionmaker,
                job=job,
                lease_owner=lease_owner,
                error="scheduler task cancelled during repository sync",
                delay=timedelta(seconds=5),
            )
            raise
        except Exception as err:
            # Queue boundary: keep the job durable and let later scheduler ticks retry it.
            _log.exception(
                "github.resync_queue.job_failed",
                repo=job.repo_full_name,
                ref=job.ref,
                generation=job.claimed_generation,
                error_type=type(err).__name__,
            )
            await _retry_job(
                sessionmaker=sessionmaker,
                job=job,
                lease_owner=lease_owner,
                error=f"setup or batch failure: {type(err).__name__}",
                delay=_retry_delay(job.attempts),
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                try:
                    await lock_connection.execute(
                        text("SELECT pg_advisory_unlock(hashtextextended(:lock_value, 0))"),
                        {"lock_value": lock_value},
                    )
                    await lock_connection.commit()
                except Exception:
                    _log.exception(
                        "github.resync_queue.repo_lock_release_failed",
                        repo=job.repo_full_name,
                        ref=job.ref,
                    )
            finally:
                await lock_connection.close()
        jobs_processed += 1

    async with sessionmaker.begin() as session:
        await resync_store.sweep_delivery_receipts(session, now=now or datetime.now(UTC))
    return jobs_processed


async def _renew_lease(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    lease_owner: uuid.UUID,
    lease_lost: asyncio.Event,
) -> None:
    while True:
        await asyncio.sleep(_LEASE_RENEW_INTERVAL.total_seconds())
        try:
            async with sessionmaker.begin() as session:
                renewed = await resync_store.renew_lease(
                    session,
                    job_id=job_id,
                    lease_owner=lease_owner,
                    lease_for=_LEASE_FOR,
                    now=datetime.now(UTC),
                )
            if not renewed:
                lease_lost.set()
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("github.resync_queue.lease_renew_failed", job_id=str(job_id))
            lease_lost.set()
            return


async def _retry_job(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    job: resync_store.GitHubPushResyncRow,
    lease_owner: uuid.UUID,
    error: str,
    delay: timedelta,
) -> None:
    current_time = datetime.now(UTC)
    async with sessionmaker.begin() as session:
        released = await resync_store.retry(
            session,
            job=job,
            lease_owner=lease_owner,
            retry_after=current_time + delay,
            error=error,
            now=current_time,
        )
        if not released:
            await resync_store.request_current_generation_pass(
                session,
                job_id=job.id,
                now=current_time,
            )


def _retry_delay(attempts: int) -> timedelta:
    seconds = min(2 ** min(max(attempts - 1, 0), 16), int(_MAX_RETRY_DELAY.total_seconds()))
    return timedelta(seconds=seconds)
