"""One-use chat grants awaiting a verified personal GitHub link."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from daimon.core._models import GitHubLinkedConnectPending
from pydantic import BaseModel, ConfigDict
from sqlalchemy import CursorResult, delete, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

# How long a claimed request stays out of reach of another `take`. A request not
# resolved by then (the process died mid-batch) can be taken again. The resume
# renews the lease of every request it still holds before each request runs.
CLAIM_LEASE = timedelta(minutes=2)
# The longest one request may run, outcome and result delivery included. The
# resume enforces it with `asyncio.timeout`; a request that hits it is answered
# with a failure line and resolved, so the next request runs. It stays well under
# the MCP service's 300 s request timeout so a small batch finishes inside it.
PER_REQUEST_DEADLINE = timedelta(seconds=60)
# The longest the failure line for a request that hit its deadline may take.
FAILURE_DELIVERY_DEADLINE = timedelta(seconds=30)
# How long a request cancelled by `PER_REQUEST_DEADLINE` keeps its HTTP client
# open for a GitHub token refresh detached from it (`settle_detached_refreshes`):
# a GitHub POST (client timeout 15 to 20 s) and the commit that stores its
# answer. Defined here so the gap bound below can count it;
# `core.github_requester_access` takes its default from this value.
DETACHED_REFRESH_SETTLE = timedelta(seconds=25)
# The longest one database transaction of the resume loop (heartbeat, take,
# resolve) may wait or run. `bound_transaction` sets it as the transaction's
# `statement_timeout` and `lock_timeout`, and the loop wraps each such
# transaction in `asyncio.timeout` of the same length, so connection checkout
# and commit are bounded too. Transactions inside a request (the authority
# read, the grant writes, recovery) carry no `asyncio.timeout` of their own;
# the request deadline bounds them.
DB_STATEMENT_DEADLINE = timedelta(seconds=15)
# A claimed row is garbage once its lease lapsed this long ago. A live callback
# renews every row it holds before each request, so the gap between two
# renewals is at most `PER_REQUEST_DEADLINE + DETACHED_REFRESH_SETTLE +
# FAILURE_DELIVERY_DEADLINE + 2 * DB_STATEMENT_DEADLINE` = 145 s: the request
# (a request cancelled at its deadline may spend `DETACHED_REFRESH_SETTLE` more
# settling a detached token refresh before the timeout returns), its failure
# line, the resolve and the next heartbeat. The lease runs `CLAIM_LEASE` past
# each renewal. Those 145 s are the enforced timeouts. Cleanup after a cancel
# comes on top and no timeout caps it (the database driver terminates a
# cancelled connection, about 2 s each), so the check below reserves
# `CANCEL_CLEANUP_ALLOWANCE` for it: an allowance, not an enforced limit. The
# safety rests on the hour of margin, not on 145 s exactly. Provided cancel
# cleanup stays within that allowance, no live holder is an hour behind and the
# gap only clears rows a crashed holder left.
CLAIMED_GARBAGE_AFTER = timedelta(hours=1)
CANCEL_CLEANUP_ALLOWANCE = timedelta(seconds=60)
RENEWAL_GAP_BOUND = (
    PER_REQUEST_DEADLINE
    + DETACHED_REFRESH_SETTLE
    + FAILURE_DELIVERY_DEADLINE
    + 2 * DB_STATEMENT_DEADLINE
)
assert RENEWAL_GAP_BOUND + CANCEL_CLEANUP_ALLOWANCE + CLAIM_LEASE < CLAIMED_GARBAGE_AFTER, (
    "a live holder must renew well inside the garbage window"
)


async def bound_transaction(session: AsyncSession) -> None:
    """Cap every statement and lock wait of this transaction at `DB_STATEMENT_DEADLINE`.

    `SET LOCAL`, so it ends with the transaction and never leaks into a pooled
    connection's next use. Call it first, before the transaction's statements.
    """
    milliseconds = int(DB_STATEMENT_DEADLINE.total_seconds() * 1000)
    await session.execute(text(f"SET LOCAL statement_timeout = {milliseconds}"))
    await session.execute(text(f"SET LOCAL lock_timeout = {milliseconds}"))


class ClaimLostError(Exception):
    """Another `take` owns this request now; the caller must not answer it."""


class PendingGrant(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    agent_id: uuid.UUID
    ma_agent_id: str
    agent_name: str
    repo_name: str
    requested_access: Literal["read", "write"]
    platform: str
    platform_user_id: str
    workspace_id: str
    parent_channel_id: str
    thread_id: str
    expires_at: datetime
    # Set by `take`: only the claim that holds this token may renew or resolve the row.
    claim_token: uuid.UUID | None = None


async def save(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    ma_agent_id: str,
    agent_name: str,
    repo_name: str,
    requested_access: Literal["read", "write"] = "read",
    platform: str,
    platform_user_id: str,
    workspace_id: str,
    parent_channel_id: str,
    thread_id: str,
) -> None:
    session.add(
        GitHubLinkedConnectPending(
            tenant_id=tenant_id,
            account_id=account_id,
            agent_id=agent_id,
            ma_agent_id=ma_agent_id,
            agent_name=agent_name,
            repo_name=repo_name,
            requested_access=requested_access,
            platform=platform,
            platform_user_id=platform_user_id,
            workspace_id=workspace_id,
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await session.flush()


async def take(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    now: datetime | None = None,
) -> list[PendingGrant]:
    """Claim this requester's unexpired requests for `CLAIM_LEASE`, oldest first.

    A claimed request is not deleted: `resolve` deletes it once it has an outcome.
    Rows another `take` claimed and has not yet released are skipped until their
    lease passes. Each returned request carries this claim's token, which
    `renew_claim`, `renew` and `resolve` check, so a lapsed claim cannot act on a
    row someone else has since taken. Expired requests are deleted if never
    claimed, or once their lease lapsed `CLAIMED_GARBAGE_AFTER` ago.

    That grace is safe as long as a live holder cannot be that far behind. Before
    each request it calls `renew_claim`, which extends the lease of every row
    it still holds. The loop's own transactions (take, renew, resolve) carry
    both `asyncio.timeout` and `bound_transaction`, each `DB_STATEMENT_DEADLINE`;
    transactions inside a request (authority read, grant writes, recovery) are
    bounded by the request deadline. Each request runs under
    `PER_REQUEST_DEADLINE`, and one cancelled at it may spend
    `DETACHED_REFRESH_SETTLE` settling a detached token refresh before the
    timeout returns. So the gap between two renewals is at most
    `PER_REQUEST_DEADLINE + DETACHED_REFRESH_SETTLE + FAILURE_DELIVERY_DEADLINE
    + 2 * DB_STATEMENT_DEADLINE` = 145 s of enforced timeouts, plus uncapped
    cancel cleanup assumed to stay within `CANCEL_CLEANUP_ALLOWANCE`. The import
    check confirms those constants sum to under the hour; it cannot measure
    elapsed time.
    A holder whose row another `take` claimed anyway finds `renew_claim` no
    longer returns it and skips that request.

    Stated limits: the resume runs inline in the OAuth callback request, so a
    crash shows the person a failed browser request and nothing retries it. Its
    requests become takeable again once the lease lapses, by the person's next
    link callback, until they expire; no scheduler recovers them. A stall of the
    event loop itself (synchronous blocking code, which no timeout can
    interrupt) is crash-equivalent: it is bounded only by the MCP service's
    request timeout, after which the person gets the browser error page the
    crash case already shows. The deploy workflow reads that timeout from the live
    service (default 300 s when unset); this repo does not configure it.
    """
    now = now or datetime.now(UTC)
    token = uuid.uuid4()
    rows = (
        await session.scalars(
            select(GitHubLinkedConnectPending)
            .where(
                GitHubLinkedConnectPending.tenant_id == tenant_id,
                GitHubLinkedConnectPending.account_id == account_id,
                GitHubLinkedConnectPending.platform == platform,
                GitHubLinkedConnectPending.platform_user_id == platform_user_id,
                GitHubLinkedConnectPending.expires_at > now,
                or_(
                    GitHubLinkedConnectPending.claimed_until.is_(None),
                    GitHubLinkedConnectPending.claimed_until <= now,
                ),
            )
            .order_by(GitHubLinkedConnectPending.expires_at, GitHubLinkedConnectPending.id)
            .with_for_update(skip_locked=True)
        )
    ).all()
    pending = [
        PendingGrant.model_validate(row).model_copy(update={"claim_token": token}) for row in rows
    ]
    if rows:
        await session.execute(
            update(GitHubLinkedConnectPending)
            .where(GitHubLinkedConnectPending.id.in_([row.id for row in rows]))
            .values(claimed_until=now + CLAIM_LEASE, claim_token=token)
            .execution_options(synchronize_session=False)
        )
    await session.execute(
        delete(GitHubLinkedConnectPending).where(
            GitHubLinkedConnectPending.expires_at <= now,
            or_(
                GitHubLinkedConnectPending.claimed_until.is_(None),
                GitHubLinkedConnectPending.claimed_until <= now - CLAIMED_GARBAGE_AFTER,
            ),
        )
    )
    return pending


async def renew_claim(
    session: AsyncSession, claim_token: uuid.UUID | None, *, now: datetime | None = None
) -> set[uuid.UUID]:
    """Extend the lease of every request this claim holds; return their ids.

    One statement, so a slow request cannot let the lease of the rows still
    waiting behind it lapse. A row missing from the result is gone or another
    claim owns it now, and the caller must not answer it.
    """
    if claim_token is None:
        return set()
    result = await session.execute(
        update(GitHubLinkedConnectPending)
        .where(GitHubLinkedConnectPending.claim_token == claim_token)
        .values(claimed_until=(now or datetime.now(UTC)) + CLAIM_LEASE)
        .returning(GitHubLinkedConnectPending.id)
    )
    return set(result.scalars())


async def renew(
    session: AsyncSession,
    request_id: uuid.UUID,
    claim_token: uuid.UUID | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Extend this claim's lease on one request; False when another claim owns it now."""
    if claim_token is None:
        return False
    result = await session.execute(
        update(GitHubLinkedConnectPending)
        .where(
            GitHubLinkedConnectPending.id == request_id,
            GitHubLinkedConnectPending.claim_token == claim_token,
        )
        .values(claimed_until=(now or datetime.now(UTC)) + CLAIM_LEASE)
    )
    return cast(CursorResult[Any], result).rowcount > 0


async def resolve(
    session: AsyncSession, request_id: uuid.UUID, claim_token: uuid.UUID | None
) -> bool:
    """Delete one request once it has an outcome, only while this claim still owns it.

    True when the row was deleted. False means it is gone or another claim took
    it, and this claim leaves it alone.
    """
    if claim_token is None:
        return False
    result = await session.execute(
        delete(GitHubLinkedConnectPending).where(
            GitHubLinkedConnectPending.id == request_id,
            GitHubLinkedConnectPending.claim_token == claim_token,
        )
    )
    return cast(CursorResult[Any], result).rowcount > 0
