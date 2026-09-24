"""Drain durable GitHub App installation repository refreshes."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from daimon.core.config import GithubSettings
from daimon.core.github_app_auth import (
    build_app_jwt,
    get_app_installation_account,
    list_installation_repositories,
    mint_installation_listing_token,
)
from daimon.core.stores import github_installation_reconciliation as reconciliation_store
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)
_LEASE_FOR = timedelta(minutes=2)
_LEASE_RENEW_INTERVAL_S = 30
_MAX_JOBS_PER_TICK = 5
_MAX_RETRY_DELAY = timedelta(minutes=5)


def _retry_delay(attempts: int) -> timedelta:
    return min(timedelta(seconds=5 * 2 ** max(0, attempts - 1)), _MAX_RETRY_DELAY)


async def drain_github_installation_reconciliations(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    github_settings: GithubSettings,
    now: datetime | None = None,
) -> int:
    """Refresh due installations; each refresh writes only its claimed generation."""
    if github_settings.app_id is None or github_settings.app_private_key is None:
        return 0

    jobs_processed = 0
    for _ in range(_MAX_JOBS_PER_TICK):
        current_time = now or datetime.now(UTC)
        lease_owner = uuid.uuid4()
        async with sessionmaker.begin() as session:
            job = await reconciliation_store.claim_due(
                session,
                lease_owner=lease_owner,
                lease_for=_LEASE_FOR,
                now=current_time,
            )
        if job is None:
            break

        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            _renew_lease(
                sessionmaker=sessionmaker,
                installation_id=job.installation_id,
                lease_owner=lease_owner,
                lease_lost=lease_lost,
            )
        )
        try:
            app_jwt = build_app_jwt(
                github_settings.app_private_key.get_secret_value(),
                github_settings.app_id,
                now=int(current_time.timestamp()),
            )
            account_login = await get_app_installation_account(
                http_client,
                jwt=app_jwt,
                installation_id=job.installation_id,
            )
            if account_login is None:
                repositories = None
            else:
                listing_token = await mint_installation_listing_token(
                    http_client,
                    jwt=app_jwt,
                    installation_id=job.installation_id,
                )
                repositories = await list_installation_repositories(
                    http_client, token=listing_token
                )

            finished_at = datetime.now(UTC)
            async with sessionmaker.begin() as session:
                completed = await reconciliation_store.finish(
                    session,
                    job=job,
                    lease_owner=lease_owner,
                    account_login=account_login,
                    repos=repositories,
                    now=finished_at,
                )
            _log.info(
                "github.installation_reconciliation.finished",
                installation_id=job.installation_id,
                generation=job.claimed_generation,
                completed=completed,
                lease_lost=lease_lost.is_set(),
                repository_count=len(repositories) if repositories is not None else 0,
            )
        except Exception as error:
            # Queue boundary: keep work durable after API, payload, or DB failures.
            _log.exception(
                "github.installation_reconciliation.failed",
                installation_id=job.installation_id,
                generation=job.claimed_generation,
                error_type=type(error).__name__,
            )
            failed_at = datetime.now(UTC)
            async with sessionmaker.begin() as session:
                await reconciliation_store.retry(
                    session,
                    job=job,
                    lease_owner=lease_owner,
                    retry_after=failed_at + _retry_delay(job.attempts),
                    error=f"{type(error).__name__}: {error}",
                    now=failed_at,
                )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        jobs_processed += 1

    async with sessionmaker.begin() as session:
        await reconciliation_store.sweep_delivery_receipts(session, now=now or datetime.now(UTC))
    return jobs_processed


async def _renew_lease(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    installation_id: int,
    lease_owner: uuid.UUID,
    lease_lost: asyncio.Event,
) -> None:
    while True:
        await asyncio.sleep(_LEASE_RENEW_INTERVAL_S)
        now = datetime.now(UTC)
        try:
            async with sessionmaker.begin() as session:
                renewed = await reconciliation_store.renew_lease(
                    session,
                    installation_id=installation_id,
                    lease_owner=lease_owner,
                    lease_for=_LEASE_FOR,
                    now=now,
                )
        except Exception:
            # Heartbeat boundary: a failed renewal is reported and the final
            # generation/owner check still prevents a stale repository write.
            _log.exception(
                "github.installation_reconciliation.lease_renewal_failed",
                installation_id=installation_id,
            )
            lease_lost.set()
            return
        if not renewed:
            lease_lost.set()
            return
