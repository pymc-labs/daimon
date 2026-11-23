"""Drain durable GitHub App installation repository refreshes."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from daimon.core.config import GithubAppSettings
from daimon.core.github_app_auth import (
    InstallationRepository,
    build_app_jwt,
    get_app_installation_account,
    get_app_installation_details,
    installation_supports_write,
    list_installation_repository_details,
    mint_installation_listing_token,
)
from daimon.core.ops_events import OpsEvent, report_ops_event
from daimon.core.stores import github_app_installations as installation_store
from daimon.core.stores import github_installation_reconciliation as reconciliation_store
from daimon.core.stores.github_follow_org import follow_new_repos
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)
_LEASE_FOR = timedelta(minutes=2)
_LEASE_RENEW_INTERVAL_S = 30
_MAX_JOBS_PER_TICK = 5
_MAX_BACKFILL_PER_TICK = 50
_MAX_RETRY_DELAY = timedelta(minutes=5)
FOLLOW_REFRESH_EVERY = timedelta(hours=1)


def _retry_delay(attempts: int) -> timedelta:
    seconds = min(5 * 2 ** min(max(attempts - 1, 0), 16), int(_MAX_RETRY_DELAY.total_seconds()))
    return timedelta(seconds=seconds)


async def drain_github_installation_reconciliations(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    github_app: GithubAppSettings,
    now: datetime | None = None,
) -> int:
    """Refresh due installations; each refresh writes only its claimed generation."""
    if github_app.app_id is None or github_app.private_key is None:
        return 0

    # Heal installations whose migration-time refresh was consumed without a
    # snapshot by a scheduler still on the previous image during the deploy.
    async with sessionmaker.begin() as session:
        queued = await reconciliation_store.enqueue_never_refreshed(
            session, now=now or datetime.now(UTC), limit=_MAX_BACKFILL_PER_TICK
        )
    if queued:
        _log.info("github.installation_reconciliation.backfill_queued", count=queued)

    # An all-repos installation sends no event for a new repo, so one an agent follows is
    # read again now and then.
    async with sessionmaker.begin() as session:
        followed = await reconciliation_store.enqueue_following_refreshes(
            session,
            now=now or datetime.now(UTC),
            older_than=FOLLOW_REFRESH_EVERY,
            limit=_MAX_BACKFILL_PER_TICK,
        )
    if followed:
        _log.info("github.installation_reconciliation.follow_queued", count=followed)

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
                github_app.private_key.get_secret_value(),
                github_app.app_id,
                now=int(current_time.timestamp()),
            )
            installation_state = await get_app_installation_account(
                http_client,
                jwt=app_jwt,
                installation_id=job.installation_id,
            )
            if installation_state is None:
                repositories = None
            else:
                async with sessionmaker() as session:
                    existing = await installation_store.get(
                        session, installation_id=job.installation_id
                    )
                    is_github_app = existing is not None and existing.app == "github_app"
                if is_github_app and installation_state.suspended_at is not None:
                    repositories = None
                else:
                    listing_token = await mint_installation_listing_token(
                        http_client,
                        jwt=app_jwt,
                        installation_id=job.installation_id,
                    )
                    listed = await list_installation_repository_details(
                        http_client, token=listing_token
                    )
                    repositories = [repo.full_name for repo in listed]
                    if installation_state.repository_selection == "all":
                        await _follow_new_repos(
                            sessionmaker=sessionmaker,
                            http_client=http_client,
                            jwt=app_jwt,
                            installation_id=job.installation_id,
                            listed=listed,
                        )

            finished_at = datetime.now(UTC)
            async with sessionmaker.begin() as session:
                completed = await reconciliation_store.finish(
                    session,
                    job=job,
                    lease_owner=lease_owner,
                    account_login=(
                        installation_state.account_login if installation_state is not None else None
                    ),
                    repos=repositories,
                    suspended_at=(
                        installation_state.suspended_at if installation_state is not None else None
                    ),
                    repository_selection=(
                        installation_state.repository_selection
                        if installation_state is not None
                        else None
                    ),
                    now=finished_at,
                )
                owner_before = (
                    await installation_store.correct_github_app_owner(
                        session,
                        installation_id=job.installation_id,
                        account_id=installation_state.account_id,
                        account_login=installation_state.account_login,
                    )
                    if completed
                    and installation_state is not None
                    and installation_state.account_id is not None
                    else None
                )
            if owner_before is not None and installation_state is not None:
                _log.info(
                    "github.installation_reconciliation.owner_corrected",
                    installation_id=job.installation_id,
                    old_account_login=owner_before[0],
                    old_account_id=owner_before[1],
                    account_login=installation_state.account_login,
                    account_id=installation_state.account_id,
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


async def _follow_new_repos(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    jwt: str,
    installation_id: int,
    listed: list[InstallationRepository],
) -> None:
    """Give agents that follow the org each new repo, before the snapshot moves on.

    A failure raises, so the job retries with the snapshot still the "before" and the same
    repos still new. An agent at its GitHub limit is reported to the workspace's admins.
    """

    async def write_access() -> bool:
        details = await get_app_installation_details(
            http_client, jwt=jwt, installation_id=installation_id
        )
        return installation_supports_write(details.permissions)

    outcome = await follow_new_repos(
        sessionmaker,
        installation_id=installation_id,
        repos=listed,
        write_access=write_access,
        now=datetime.now(UTC),
    )
    for limit in outcome.limits:
        report_ops_event(
            sessionmaker,
            OpsEvent(
                code="github.follow_limit",
                tenant_id=limit.tenant_id,
                agent_id=limit.ma_agent_id,
                reason=limit.reason,
            ),
        )
    if outcome.granted or outcome.renamed or outcome.limits:
        _log.info(
            "github.installation_reconciliation.followed",
            installation_id=installation_id,
            granted=outcome.granted,
            renamed=outcome.renamed,
            at_limit=len(outcome.limits),
        )


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
