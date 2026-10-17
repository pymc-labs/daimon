"""Keep linked GitHub user tokens alive so a person links once.

The personal token is refreshed whenever Connect uses it, but someone who stops
adding repositories would reach the six-month refresh-token expiry and have to
link again. This sweep spends each refresh token that is close to expiry through
the same serialized refresh path Connect uses, which gives the row a new one.
"""

from __future__ import annotations

import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
import structlog
from cryptography.fernet import MultiFernet
from daimon.core.github_requester_access import linked_access_token_with_generation
from daimon.core.stores.github_links import (
    MAX_EXCLUDED_USER_IDS,
    list_users_with_expiring_refresh,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

# How long one person's refresh waits for the refresh lock another worker holds
# (a Postgres interval). The GitHub call is bounded by the HTTP client's timeout.
LOCK_TIMEOUT = "15s"
# How long a failed person is left out of the sweep, so persistent failures
# cannot hold every slot. Long, because an hourly sweep that retried them after a
# few hours would cycle the same failures back in front of a healthy later row.
FAILURE_BACKOFF = timedelta(hours=24)


@dataclass(frozen=True)
class LinkRefreshCounts:
    refreshed: int = 0
    broken: int = 0
    failed: int = 0
    # People selected but not started because the budget ran out or a stop was
    # asked for. They are not backed off: they stay first in line.
    not_attempted: int = 0


async def refresh_expiring_links(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    *,
    fernet: MultiFernet,
    client_id: str,
    client_secret: str,
    now: datetime,
    window: timedelta = timedelta(days=30),
    limit: int = 50,
    backoff: MutableMapping[int, datetime] | None = None,
    failure_backoff: timedelta = FAILURE_BACKOFF,
    lock_timeout: str | None = LOCK_TIMEOUT,
    budget_s: float | None = None,
    should_stop: Callable[[], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> LinkRefreshCounts:
    """Refresh up to ``limit`` active links whose refresh token expires within ``window``.

    A link whose refresh token GitHub rejects becomes broken, as on any Connect
    use. A transport or 429/5xx failure, or a wait for the refresh lock longer
    than ``lock_timeout`` (SQLSTATE 55P03), counts as failed. Failures are logged
    by GitHub user id only.

    ``backoff`` maps a GitHub user id to when it may be tried again. A failed
    person is entered there for ``failure_backoff``, and anyone still backed off
    is left out of the selection, so persistent failures cannot occupy every
    slot. The caller keeps the map between sweeps.

    Nothing here cancels a person's refresh: GitHub retires the old tokens when
    it answers, so a refresh that began is finished and stored. ``budget_s`` and
    ``should_stop`` are checked only before starting each person; once either
    says stop, the rest are counted ``not_attempted`` and left out of the
    backoff. One person's refresh is bounded by ``lock_timeout``, the client's
    HTTP timeout and database time.
    """
    started = monotonic()
    before = now + window
    excluded: set[int] = set()
    if backoff is not None:
        for user_id, retry_after in list(backoff.items()):
            if retry_after <= now:
                del backoff[user_id]
        while len(backoff) > MAX_EXCLUDED_USER_IDS:
            del backoff[min(backoff, key=lambda user_id: backoff[user_id])]
        excluded = set(backoff)
    async with sessionmaker() as session:
        user_ids = await list_users_with_expiring_refresh(
            session, now=now, before=before, limit=limit, exclude_user_ids=excluded
        )
    refreshed = broken = failed = 0
    for position, user_id in enumerate(user_ids):
        if (should_stop is not None and should_stop()) or (
            budget_s is not None and monotonic() - started >= budget_s
        ):
            return LinkRefreshCounts(
                refreshed=refreshed,
                broken=broken,
                failed=failed,
                not_attempted=len(user_ids) - position,
            )
        try:
            token, _ = await linked_access_token_with_generation(
                sessionmaker,
                client,
                user_id=user_id,
                fernet=fernet,
                client_id=client_id,
                client_secret=client_secret,
                refresh_if_refresh_expires_before=before,
                lock_timeout=lock_timeout,
            )
        except Exception as exc:
            failed += 1
            if backoff is not None:
                backoff[user_id] = now + failure_backoff
            log.warning(
                "github_link_refresh.failed",
                github_user_id=user_id,
                error_type=type(exc).__name__,
                sqlstate=getattr(getattr(exc, "orig", None), "sqlstate", None),
                status_code=exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None,
            )
            continue
        if token is None:
            broken += 1
            log.info("github_link_refresh.broken", github_user_id=user_id)
        else:
            refreshed += 1
    return LinkRefreshCounts(refreshed=refreshed, broken=broken, failed=failed)
