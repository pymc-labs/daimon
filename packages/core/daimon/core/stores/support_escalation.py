"""Async store for support_escalations — human-support requests and their credit gate.

No try/except anywhere in this module — exceptions propagate to the adapter
boundary. Callers own the transaction; every write ends with `await
session.flush()`.

The credit gate lives in `record_escalation` rather than in the caller, and it
counts inside the caller's transaction, because a check performed before the
transaction is a TOCTOU: two fast clicks both read `used=2` against an
allowance of 3, both decide they are permitted, and both insert. Before it
counts, `record_escalation` takes a transaction-scoped advisory lock on the
(tenant, user) pair, so a second request for the same person waits until the
first commits or rolls back and then counts its row: the count and the insert
are atomic under `READ COMMITTED` too, not just narrowed to a statement pair.

`record_escalation_once` adds the double-submit guard: under the same lock it
refuses a second request from the same person on the same message, so a
double click or a platform retry neither spends a second credit nor posts
twice.

Support plans (`daimon.core.support_plans`) make the rows a credit ledger:
each is charged to the `default` ledger, a `plan`, or nothing (`exempt`), and
spends `COALESCE(charge, hold)`. `submit_request` and `settle_request` are the
plan-aware writers. Lock order, each transaction-scoped and never reversed:
the plan (`daimon:support_plan:<id>`), the person (above), then the tenant
(`lock_access_policy`). Config writers take only the tenant lock and settles
only the first two, so no two paths wait on each other in opposite orders.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal, cast

from daimon.core._models import SupportEscalation
from daimon.core.stores.access_policy import lock_access_policy
from daimon.core.stores.support_plans import (
    get_allowance,
    get_catalog_by_id,
    get_plan_by_id,
    plans_for_place,
)
from daimon.core.stores.support_rows import PlanView, SupportEscalationRow, SupportPlanRow
from daimon.core.support_escalation import has_credit, remaining_credits
from daimon.core.support_plans import (
    DEFAULT_OPTION_KEY,
    Balance,
    RequestChoice,
    RequestStatus,
    SettleProblem,
    governing_plan,
    hold_for,
    may_submit,
    offered_option,
    request_note,
    settle_problem,
    settles_at_submit,
    snapshot_option,
    validate_answers,
)
from daimon.core.support_plans import SupportPlace as SupportPlace
from daimon.core.windows import WindowPeriod, window_period, window_state
from sqlalchemy import (
    ColumnElement,
    CursorResult,
    delete,
    false,
    func,
    insert,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

# Namespace prefix for the pg_advisory_xact_lock key, so this ledger's keys
# never collide with another advisory-lock user in the same database.
_LOCK_NAMESPACE = "daimon:support_escalation:"


_PLAN_LOCK_NAMESPACE = "daimon:support_plan:"
PURGED_USER: Final = "purged"
"""The requester of a plan row whose person was erased; it keeps its charge (decision 7)."""
_COUNTED = func.coalesce(SupportEscalation.charge, SupportEscalation.hold)
_IS_PLAN = SupportEscalation.charged_to == "plan"
_NOT_PLAN = SupportEscalation.charged_to != "plan"
_ANONYMOUS: Final[dict[str, Any]] = {
    "platform_user_id": PURGED_USER,
    "account_id": None,
    "ma_session_id": None,
    "note": "",
    "answers": None,
}


async def _advisory_lock(session: AsyncSession, key: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
    )


async def _lock_plan(session: AsyncSession, *, plan_id: uuid.UUID) -> None:
    """Serialize every charge against one plan's pool until the transaction ends."""
    await _advisory_lock(session, f"{_PLAN_LOCK_NAMESPACE}{plan_id}")


async def lock_plan(session: AsyncSession, *, plan_id: uuid.UUID) -> None:
    """Take a plan's charge lock. It comes before the tenant lock, as in `submit_request`."""
    await _lock_plan(session, plan_id=plan_id)


async def _lock_user_ledger(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform_user_id: str
) -> None:
    """Serialize every escalation write for one (tenant, user) until the transaction ends.

    Blocking and transaction-scoped: a concurrent request for the same person
    waits, then counts (and sees) the row the first one committed.
    """
    await _advisory_lock(session, f"{_LOCK_NAMESPACE}{tenant_id}:{platform_user_id}")


async def default_spent(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform_user_id: str
) -> Decimal:
    """What this person has spent on the default ledger in this tenant, ever.

    Credits are per user WITHIN a tenant, so both columns are always in the
    predicate — the same person in two installs has two separate allowances.
    Plan and exempt rows never count here.
    """
    stmt = select(func.coalesce(func.sum(_COUNTED), 0)).where(
        SupportEscalation.tenant_id == tenant_id,
        SupportEscalation.platform_user_id == platform_user_id,
        SupportEscalation.charged_to == "default",
    )
    return Decimal((await session.execute(stmt)).scalar_one())


async def count_escalations_for_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
) -> int:
    """How many default credits this person has spent: `default_spent`, rounded up.

    Every row an older writer made is charged 1, so this is the old row count.
    """
    return math.ceil(
        await default_spent(session, tenant_id=tenant_id, platform_user_id=platform_user_id)
    )


async def record_escalation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    platform: str,
    platform_user_id: str,
    channel_id: str,
    message_id: str,
    ma_session_id: str | None,
    note: str,
    allowance: int,
) -> SupportEscalationRow | None:
    """Insert one escalation if the person has a credit left, else `None`.

    `None` means "out of credits", which the caller renders as the contact-us
    message. It is not an error and must not be raised: running out is the
    designed end of a trial allowance, not a fault.

    `delivered_at` is left NULL. The caller attempts operator delivery AFTER
    this returns and calls `mark_delivered` only once a DM actually lands, so
    a request whose delivery fails entirely survives as an undelivered row.
    Committing the row first is deliberate and is the reason this function does
    not take a delivery callback.
    """
    await _lock_user_ledger(session, tenant_id=tenant_id, platform_user_id=platform_user_id)
    used = await count_escalations_for_user(
        session, tenant_id=tenant_id, platform_user_id=platform_user_id
    )
    if not has_credit(allowance=allowance, used=used):
        return None

    orm = SupportEscalation(
        tenant_id=tenant_id,
        account_id=account_id,
        platform=platform,
        platform_user_id=platform_user_id,
        channel_id=channel_id,
        message_id=message_id,
        ma_session_id=ma_session_id,
        note=note,
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return SupportEscalationRow.model_validate(orm)


EscalationStatus = Literal["recorded", "duplicate", "out_of_credits", "refused"]


@dataclass(frozen=True)
class EscalationOutcome:
    """What `record_escalation_once` did, and how many credits are left after it.

    `row` is the new row for "recorded" and None otherwise. `remaining` is
    counted inside the same transaction, so the confirmation shows the count
    the write actually left rather than a later re-read. "refused" means the
    caller's `source_allowed` check said no; nothing was written or counted
    (`remaining` is then -1, meaning "not read").
    """

    status: EscalationStatus
    row: SupportEscalationRow | None
    remaining: int


async def find_escalation_for_message(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    channel_id: str,
    message_id: str,
) -> SupportEscalationRow | None:
    """This person's existing request on one message, if any."""
    stmt = (
        select(SupportEscalation)
        .where(
            SupportEscalation.tenant_id == tenant_id,
            SupportEscalation.platform == platform,
            SupportEscalation.platform_user_id == platform_user_id,
            SupportEscalation.channel_id == channel_id,
            SupportEscalation.message_id == message_id,
        )
        .limit(1)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else SupportEscalationRow.model_validate(orm)


async def record_escalation_once(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    platform: str,
    platform_user_id: str,
    channel_id: str,
    message_id: str,
    ma_session_id: str | None,
    note: str,
    allowance: int,
    source_allowed: Callable[[AsyncSession], Awaitable[bool]] | None = None,
) -> EscalationOutcome:
    """`record_escalation`, refusing a second request from one person on one message.

    `source_allowed` is the caller's authoritative access decision for the
    place the request comes from. It runs inside this transaction AFTER both
    locks are held, so it must read the policy itself (fresh): lock order is
    the per-person ledger lock, then the tenant policy lock
    (`lock_access_policy`, FOR NO KEY UPDATE on the tenant row), each held to
    the end of the caller's transaction. A policy edit committed while this
    waited for the ledger lock is therefore read and refuses, with nothing
    spent; an edit arriving once the policy lock is held waits for the commit.
    No holder of the tenant lock (the policy writers, a private form's consume)
    ever takes a ledger lock, and the insert's foreign-key check takes only KEY
    SHARE, which NO KEY UPDATE does not block, so the order cannot deadlock.

    The duplicate check, the credit count and the insert all run under the
    (tenant, user) lock in the caller's transaction, so two submissions racing
    on one message record one row and spend one credit; the loser reads the
    winner's committed row and reports "duplicate". A duplicate is checked
    before credits so someone who spent their last credit on this very
    message hears that it is in hand, not that they are out.
    """
    await _lock_user_ledger(session, tenant_id=tenant_id, platform_user_id=platform_user_id)
    if source_allowed is not None:
        await lock_access_policy(session, tenant_id=tenant_id)
        if not await source_allowed(session):
            return EscalationOutcome(status="refused", row=None, remaining=-1)
    existing = await find_escalation_for_message(
        session,
        tenant_id=tenant_id,
        platform=platform,
        platform_user_id=platform_user_id,
        channel_id=channel_id,
        message_id=message_id,
    )
    if existing is not None:
        used = await count_escalations_for_user(
            session, tenant_id=tenant_id, platform_user_id=platform_user_id
        )
        return EscalationOutcome(
            status="duplicate",
            row=None,
            remaining=remaining_credits(allowance=allowance, used=used),
        )
    row = await record_escalation(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        platform=platform,
        platform_user_id=platform_user_id,
        channel_id=channel_id,
        message_id=message_id,
        ma_session_id=ma_session_id,
        note=note,
        allowance=allowance,
    )
    used = await count_escalations_for_user(
        session, tenant_id=tenant_id, platform_user_id=platform_user_id
    )
    return EscalationOutcome(
        status="out_of_credits" if row is None else "recorded",
        row=row,
        remaining=remaining_credits(allowance=allowance, used=used),
    )


async def mark_delivered(
    session: AsyncSession,
    *,
    escalation_id: uuid.UUID,
    support_channel_id: str | None = None,
    support_post_id: str | None = None,
) -> SupportEscalationRow | None:
    """Stamp `delivered_at` once an operator DM has actually landed.

    Idempotent in effect — re-stamping refreshes the timestamp rather than
    failing — because the alternative (predicating on `delivered_at IS NULL`)
    would make a retry look like a missing row to the caller. The support
    post's ids, when given, let a settle edit that post.
    """
    values: dict[str, Any] = {"delivered_at": func.now()}
    if support_channel_id is not None:
        values["support_channel_id"] = support_channel_id
    if support_post_id is not None:
        values["support_post_id"] = support_post_id
    stmt = (
        update(SupportEscalation)
        .where(SupportEscalation.id == escalation_id)
        .values(**values)
        .returning(SupportEscalation)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    if orm is None:
        return None
    return SupportEscalationRow.model_validate(orm)


async def delete_support_escalations_for_platform_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
) -> int:
    """Delete one platform user's escalations in one tenant, but for plan rows. Idempotent.

    Tenant-scoped for the same reason the message_feedback counterpart is:
    `platform_user_id` is not globally unique across platforms, and purging
    ONE principal must not erase the same person's rows in another install.
    """
    result = await session.execute(
        delete(SupportEscalation).where(
            SupportEscalation.tenant_id == tenant_id,
            SupportEscalation.platform_user_id == platform_user_id,
            _NOT_PLAN,
        )
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def anonymize_plan_requests_for_platform_user(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform_user_id: str
) -> int:
    """Erase the person from their plan rows and keep the charges (decision 7). Idempotent.

    A shared pool is not refunded by one member leaving. The option snapshot
    is the tenant's menu, not the person's words, so it stays.
    """
    result = await session.execute(
        update(SupportEscalation)
        .where(
            SupportEscalation.tenant_id == tenant_id,
            SupportEscalation.platform_user_id == platform_user_id,
            _IS_PLAN,
        )
        .values(**_ANONYMOUS)
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


def _account_or_platform_user_predicate(
    *, account_id: uuid.UUID, platform_user_keys: Sequence[tuple[uuid.UUID, str]]
) -> ColumnElement[bool]:
    """Shared predicate for the account-scoped delete/count pair.

    They MUST stay identical, or the /privacy cascade preview lies about what
    erasure will remove. Same shape and same reason as the message_feedback
    pair: a request written before the person had an `accounts` row carries
    `account_id = NULL` and is unreachable by an account-keyed delete alone,
    so the `(tenant_id, platform_user_id)` membership check closes that gap.
    """
    predicates: list[ColumnElement[bool]] = [SupportEscalation.account_id == account_id]
    if platform_user_keys:
        predicates.append(
            tuple_(SupportEscalation.tenant_id, SupportEscalation.platform_user_id).in_(
                list(platform_user_keys)
            )
        )
    return or_(*predicates)


async def delete_support_escalations_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    platform_user_keys: Sequence[tuple[uuid.UUID, str]],
) -> int:
    """Delete every escalation attributable to `account_id`, but for plan rows. Idempotent."""
    predicate = _account_or_platform_user_predicate(
        account_id=account_id, platform_user_keys=platform_user_keys
    )
    result = await session.execute(delete(SupportEscalation).where(predicate, _NOT_PLAN))
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def count_support_escalations_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    platform_user_keys: Sequence[tuple[uuid.UUID, str]],
) -> int:
    """Count what `delete_support_escalations_for_account` would delete. Read-only."""
    predicate = _account_or_platform_user_predicate(
        account_id=account_id, platform_user_keys=platform_user_keys
    )
    stmt = select(func.count()).select_from(SupportEscalation).where(predicate, _NOT_PLAN)
    return int((await session.execute(stmt)).scalar_one())


async def anonymize_plan_requests_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    platform_user_keys: Sequence[tuple[uuid.UUID, str]],
) -> int:
    """`anonymize_plan_requests_for_platform_user` over the account predicate. Idempotent."""
    predicate = _account_or_platform_user_predicate(
        account_id=account_id, platform_user_keys=platform_user_keys
    )
    stmt = update(SupportEscalation).where(predicate, _IS_PLAN).values(**_ANONYMOUS)
    result = await session.execute(stmt)
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def count_plan_requests_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    platform_user_keys: Sequence[tuple[uuid.UUID, str]],
) -> int:
    """Count what `anonymize_plan_requests_for_account` would anonymize. Read-only."""
    predicate = _account_or_platform_user_predicate(
        account_id=account_id, platform_user_keys=platform_user_keys
    )
    stmt = select(func.count()).select_from(SupportEscalation).where(predicate, _IS_PLAN)
    return int((await session.execute(stmt)).scalar_one())


def _settler_predicate(
    *, account_id: uuid.UUID | None, settler_keys: Sequence[tuple[str, str]]
) -> ColumnElement[bool]:
    """Rows a person settled: by account, or by (platform, platform user id) in any tenant.

    Any tenant, because the support channel may sit in another guild or workspace.
    """
    predicates: list[ColumnElement[bool]] = []
    if account_id is not None:
        predicates.append(SupportEscalation.settled_by_account_id == account_id)
    if settler_keys:
        predicates.append(
            tuple_(SupportEscalation.platform, SupportEscalation.settled_by).in_(list(settler_keys))
        )
    return or_(false(), *predicates)


async def clear_settler(
    session: AsyncSession,
    *,
    account_id: uuid.UUID | None = None,
    settler_keys: Sequence[tuple[str, str]] = (),
) -> int:
    """Staff purge: drop who settled other people's requests, keep the charges. Idempotent."""
    predicate = _settler_predicate(account_id=account_id, settler_keys=settler_keys)
    stmt = (
        update(SupportEscalation)
        .where(predicate)
        .values(settled_by=None, settled_by_account_id=None)
    )
    result = await session.execute(stmt)
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def count_settled_by(
    session: AsyncSession,
    *,
    account_id: uuid.UUID | None = None,
    settler_keys: Sequence[tuple[str, str]] = (),
) -> int:
    """Count what `clear_settler` would clear. Read-only."""
    predicate = _settler_predicate(account_id=account_id, settler_keys=settler_keys)
    stmt = select(func.count()).select_from(SupportEscalation).where(predicate)
    return int((await session.execute(stmt)).scalar_one())


# ---------- Plans: balances, submit, settle ----------


def _month_after(start: datetime) -> datetime:
    return datetime(start.year + start.month // 12, start.month % 12 + 1, 1, tzinfo=UTC)


def _period(plan: SupportPlanRow, *, as_of: datetime) -> WindowPeriod:
    """The window holding `as_of`; a monthly window ends with its month, for past rows."""
    period = window_period(plan, now=as_of)
    if plan.window == "monthly" and period.since is not None:
        return WindowPeriod(since=period.since, until=_month_after(period.since))
    return period


async def plan_spent(
    session: AsyncSession, *, plan: SupportPlanRow, platform_user_id: str, as_of: datetime
) -> tuple[Decimal, Decimal]:
    """(the pool's spend, this person's spend) in the plan's window holding `as_of`."""
    period = _period(plan, as_of=as_of)
    where: list[ColumnElement[bool]] = [
        SupportEscalation.tenant_id == plan.tenant_id,
        SupportEscalation.plan_id == plan.id,
        _IS_PLAN,
    ]
    if period.since is not None:
        where.append(SupportEscalation.created_at >= period.since)
    if period.until is not None:
        where.append(SupportEscalation.created_at < period.until)
    person = func.sum(_COUNTED).filter(SupportEscalation.platform_user_id == platform_user_id)
    stmt = select(func.coalesce(func.sum(_COUNTED), 0), func.coalesce(person, 0)).where(*where)
    pool_used, person_used = (await session.execute(stmt)).one()
    return Decimal(pool_used), Decimal(person_used)


async def _balance(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    plan: SupportPlanRow | None,
    platform_user_id: str,
    default_allowance: int,
    as_of: datetime,
) -> Balance:
    """Both axes for one person; an allowance row replaces the plan's per-person cap."""
    allowance = await get_allowance(
        session,
        tenant_id=tenant_id,
        plan_id=None if plan is None else plan.id,
        platform_user_id=platform_user_id,
    )
    if plan is None:
        cap = Decimal(default_allowance) if allowance is None else allowance.credits
        used = await default_spent(session, tenant_id=tenant_id, platform_user_id=platform_user_id)
        return Balance(pool=None, pool_used=Decimal(0), cap=cap, person_used=used)
    cap = plan.per_person if allowance is None else allowance.credits
    if platform_user_id == PURGED_USER:
        cap = None  # erased people share one id; only the pool still binds
    pool_used, person_used = await plan_spent(
        session, plan=plan, platform_user_id=platform_user_id, as_of=as_of
    )
    return Balance(pool=plan.pool, pool_used=pool_used, cap=cap, person_used=person_used)


async def _plan_view(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    plan: SupportPlanRow | None,
    platform_user_id: str,
    default_allowance: int,
    exempt: bool,
    now: datetime,
) -> PlanView:
    catalog = None
    if plan is not None and plan.catalog_id is not None:
        catalog = await get_catalog_by_id(session, tenant_id=tenant_id, catalog_id=plan.catalog_id)
    balance = await _balance(
        session,
        tenant_id=tenant_id,
        plan=plan,
        platform_user_id=platform_user_id,
        default_allowance=default_allowance,
        as_of=now,
    )
    state = "active" if plan is None else window_state(plan, now=now)
    return PlanView(plan=plan, catalog=catalog, balance=balance, state=state, exempt=exempt)


async def _governing(
    session: AsyncSession, *, tenant_id: uuid.UUID, place: SupportPlace
) -> SupportPlanRow | None:
    plans = await plans_for_place(session, tenant_id=tenant_id, place=place)
    return governing_plan(plans, place)


async def resolve_plan(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    place: SupportPlace,
    platform_user_id: str,
    default_allowance: int,
    exempt: bool = False,
    now: datetime | None = None,
) -> PlanView:
    """The plan, menu and balance for one person in one place, read now (decision 9).

    `default_allowance` is `settings.support.credits_per_user`. `exempt` is the
    adapter's native-admin check for the requester (decision 5).
    """
    plan = await _governing(session, tenant_id=tenant_id, place=place)
    return await _plan_view(
        session,
        tenant_id=tenant_id,
        plan=plan,
        platform_user_id=platform_user_id,
        default_allowance=default_allowance,
        exempt=exempt,
        now=now or datetime.now(UTC),
    )


SubmitStatus = Literal[
    "recorded", "duplicate", "out_of_credits", "stale", "refused", "plan_inactive"
]


@dataclass(frozen=True)
class SubmitOutcome:
    """What `submit_request` did.

    `row` is the new row for "recorded" and the person's earlier one for
    "duplicate". `view` is the plan read under the locks, with the new row
    spent; None for "refused", where nothing was read.
    """

    status: SubmitStatus
    row: SupportEscalationRow | None
    view: PlanView | None


async def _find_request(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    channel_id: str,
    message_id: str,
    option_key: str,
) -> SupportEscalationRow | None:
    """One request per (message, person, option); a legacy row is the default option."""
    stmt = (
        select(SupportEscalation)
        .where(
            SupportEscalation.tenant_id == tenant_id,
            SupportEscalation.platform == platform,
            SupportEscalation.platform_user_id == platform_user_id,
            SupportEscalation.channel_id == channel_id,
            SupportEscalation.message_id == message_id,
            func.coalesce(SupportEscalation.option_key, DEFAULT_OPTION_KEY) == option_key,
        )
        .limit(1)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else SupportEscalationRow.model_validate(orm)


async def asked_options(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    channel_id: str,
    message_id: str,
) -> frozenset[str]:
    """The options this person already asked with on one message; a legacy row is the default."""
    stmt = select(func.coalesce(SupportEscalation.option_key, DEFAULT_OPTION_KEY)).where(
        SupportEscalation.tenant_id == tenant_id,
        SupportEscalation.platform == platform,
        SupportEscalation.platform_user_id == platform_user_id,
        SupportEscalation.channel_id == channel_id,
        SupportEscalation.message_id == message_id,
    )
    return frozenset((await session.scalars(stmt)).all())


async def submit_request(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    platform: str,
    platform_user_id: str,
    channel_id: str,
    message_id: str,
    ma_session_id: str | None,
    place: SupportPlace,
    choice: RequestChoice,
    default_allowance: int,
    exempt: bool = False,
    source_allowed: Callable[[AsyncSession], Awaitable[bool]] | None = None,
    now: datetime | None = None,
) -> SubmitOutcome:
    """Record one request on the governing plan, if its terms still stand and it is affordable.

    Locks the plan (unless `exempt`, which spends nothing), the person and the
    tenant, then re-reads everything under them, so the balance, the menu and
    the duplicate check are atomic with the insert. "stale" means the plan, or
    the picked option's price or fields, changed since it was shown
    (`OPTIONS_CHANGED`); a label-only change records the option as it is now. A bad step or
    answer raises `SupportPlanError`; adapters check them on the menu first.
    `source_allowed` is as in `record_escalation_once`.
    """
    now = now or datetime.now(UTC)
    shown = await _governing(session, tenant_id=tenant_id, place=place)
    if shown is not None and not exempt:
        await _lock_plan(session, plan_id=shown.id)
    await _lock_user_ledger(session, tenant_id=tenant_id, platform_user_id=platform_user_id)
    await lock_access_policy(session, tenant_id=tenant_id)
    if source_allowed is not None and not await source_allowed(session):
        return SubmitOutcome(status="refused", row=None, view=None)
    plan = await _governing(session, tenant_id=tenant_id, place=place)
    view = await _plan_view(
        session,
        tenant_id=tenant_id,
        plan=plan,
        platform_user_id=platform_user_id,
        default_allowance=default_allowance,
        exempt=exempt,
        now=now,
    )
    existing = await _find_request(
        session,
        tenant_id=tenant_id,
        platform=platform,
        platform_user_id=platform_user_id,
        channel_id=channel_id,
        message_id=message_id,
        option_key=choice.option_key,
    )
    if existing is not None:
        return SubmitOutcome(status="duplicate", row=existing, view=view)
    option = offered_option(view.spec, choice.option_key, choice.terms)
    same_plan = (None if plan is None else plan.id) == (None if shown is None else shown.id)
    if not same_plan or option is None:
        return SubmitOutcome(status="stale", row=None, view=view)
    snapshot = snapshot_option(option, step=choice.step)
    answers = validate_answers(option, choice.answers)
    if not view.active:
        return SubmitOutcome(status="plan_inactive", row=None, view=view)
    bounds = snapshot.bounds
    if not exempt and not may_submit(bounds, view.available):
        return SubmitOutcome(status="out_of_credits", row=None, view=view)
    hold = Decimal(0) if exempt else hold_for(bounds)
    settled = exempt or settles_at_submit(option.price)
    stmt = (
        insert(SupportEscalation)
        .values(
            tenant_id=tenant_id,
            account_id=account_id,
            platform=platform,
            platform_user_id=platform_user_id,
            channel_id=channel_id,
            message_id=message_id,
            ma_session_id=ma_session_id,
            note=request_note(option, answers),
            charged_to=view.charged_to,
            plan_id=None if plan is None else plan.id,
            option_key=option.key,
            option=snapshot.model_dump(mode="json", exclude_none=True),
            answers=answers,
            hold=hold,
            charge=hold if settled else None,
            status="settled" if settled else "pending",
        )
        .returning(SupportEscalation)
    )
    row = SupportEscalationRow.model_validate((await session.execute(stmt)).scalar_one())
    await session.flush()
    if not exempt:
        balance = replace(
            view.balance,
            pool_used=view.balance.pool_used + hold,
            person_used=view.balance.person_used + hold,
        )
        view = replace(view, balance=balance)
    return SubmitOutcome(status="recorded", row=row, view=view)


async def get_request(
    session: AsyncSession, *, escalation_id: uuid.UUID, tenant_id: uuid.UUID | None = None
) -> SupportEscalationRow | None:
    """One request; `tenant_id` None is the support post's path, which may cross tenants."""
    stmt = select(SupportEscalation).where(SupportEscalation.id == escalation_id)
    if tenant_id is not None:
        stmt = stmt.where(SupportEscalation.tenant_id == tenant_id)
    orm = (
        await session.execute(stmt.execution_options(populate_existing=True))
    ).scalar_one_or_none()
    return None if orm is None else SupportEscalationRow.model_validate(orm)


async def list_requests(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: RequestStatus | None = None,
    plan_id: uuid.UUID | None = None,
    limit: int = 50,
) -> list[SupportEscalationRow]:
    """Newest first."""
    stmt = select(SupportEscalation).where(SupportEscalation.tenant_id == tenant_id)
    if status is not None:
        stmt = stmt.where(SupportEscalation.status == status)
    if plan_id is not None:
        stmt = stmt.where(SupportEscalation.plan_id == plan_id)
    stmt = stmt.order_by(SupportEscalation.created_at.desc()).limit(limit)
    return [SupportEscalationRow.model_validate(o) for o in (await session.scalars(stmt)).all()]


SettleStatus = Literal[
    "settled",
    "declined",
    "already_settled",
    "stale",
    "invalid",
    "over_balance",
    "plan_gone",
    "not_found",
]


@dataclass(frozen=True)
class SettleOutcome:
    """What `settle_request` did; `row` is the request as it now stands (None if not found).

    `available` is the balance left on the row's ledger with the row's current
    charge spent, None when unbounded or not read (a decline, an orphan).
    "invalid" and "over_balance" pair with `settle_range_text`. "plan_gone" is a
    charge on a request whose plan was removed: it has no balance to check, so
    only a decline (or a correction down) goes through.
    """

    status: SettleStatus
    row: SupportEscalationRow | None
    available: Decimal | None = None


async def _ledger_available(
    session: AsyncSession, *, row: SupportEscalationRow, default_allowance: int
) -> Decimal | None:
    """The balance left on the row's ledger in the window it was made in; None if unbounded."""
    plan = None
    if row.charged_to == "plan":
        if row.plan_id is None:
            return None  # its plan was cleared: it counts nowhere
        plan = await get_plan_by_id(session, tenant_id=row.tenant_id, plan_id=row.plan_id)
        if plan is None:
            return None
    balance = await _balance(
        session,
        tenant_id=row.tenant_id,
        plan=plan,
        platform_user_id=row.platform_user_id,
        default_allowance=default_allowance,
        as_of=row.created_at,
    )
    return balance.available


async def decline_pending_for_plan(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    settled_by: str | None,
    settled_by_account_id: uuid.UUID | None,
) -> list[SupportEscalationRow]:
    """Decline every pending request of a plan about to be removed, at no charge.

    The caller holds the plan's lock (`lock_plan`), which keeps settles and new
    requests off it. A request left pending would settle with no balance to
    bound it once the plan row is gone.
    """
    stmt = (
        update(SupportEscalation)
        .where(SupportEscalation.plan_id == plan_id, SupportEscalation.status == "pending")
        .values(
            charge=Decimal(0),
            status="declined",
            settled_at=func.now(),
            settled_by=settled_by,
            settled_by_account_id=settled_by_account_id,
        )
        .returning(SupportEscalation)
        .execution_options(populate_existing=True)
    )
    rows = (await session.execute(stmt)).scalars().all()
    await session.flush()
    return [SupportEscalationRow.model_validate(o) for o in rows]


async def settle_request(
    session: AsyncSession,
    *,
    escalation_id: uuid.UUID,
    charge: Decimal | None,
    settled_by: str | None,
    settled_by_account_id: uuid.UUID | None,
    default_allowance: int,
    tenant_id: uuid.UUID | None = None,
    expected_charge: Decimal | None = None,
) -> SettleOutcome:
    """Settle a pending request at `charge`, or decline it (`charge` None: charged 0).

    `expected_charge` re-settles a settled or declined row instead (an admin's
    refund or correction, down to 0), and is "stale" unless the row still
    charges exactly that. Locks the row's plan, then the requester, so a
    second click waits and reads "already_settled". `tenant_id` None is the
    support post's path. A settle may come after the window ended; the
    balance is the one of the window the request was made in.
    """
    row = await get_request(session, escalation_id=escalation_id, tenant_id=tenant_id)
    if row is None:
        return SettleOutcome(status="not_found", row=None)
    if row.charged_to == "plan" and row.plan_id is not None:
        await _lock_plan(session, plan_id=row.plan_id)
    await _lock_user_ledger(session, tenant_id=row.tenant_id, platform_user_id=row.platform_user_id)
    row = await get_request(session, escalation_id=escalation_id, tenant_id=tenant_id)
    if row is None:
        return SettleOutcome(status="not_found", row=None)
    if row.charged_to == "exempt":
        return SettleOutcome(status="invalid", row=row)
    correction = expected_charge is not None
    if not correction and row.status != "pending":
        return SettleOutcome(status="already_settled", row=row)
    if correction and (row.status == "pending" or row.charge != expected_charge):
        return SettleOutcome(status="stale", row=row)
    if (
        row.charged_to == "plan"
        and row.plan_id is None
        and charge is not None
        and not (correction and charge <= (row.charge or Decimal(0)))
    ):
        return SettleOutcome(status="plan_gone", row=row)
    available = None
    if charge is not None:
        available = await _ledger_available(session, row=row, default_allowance=default_allowance)
        problem: SettleProblem | None = settle_problem(
            row.bounds,
            charge=charge,
            counted=row.counted,
            available=available,
            correction=correction,
        )
        if problem is not None:
            return SettleOutcome(status=problem, row=row, available=available)
    final = Decimal(0) if charge is None else charge
    status: RequestStatus = "declined" if charge is None else "settled"
    stmt = (
        update(SupportEscalation)
        .where(
            SupportEscalation.id == row.id,
            SupportEscalation.status == row.status,
            SupportEscalation.charge.is_not_distinct_from(row.charge),
        )
        .values(
            charge=final,
            status=status,
            settled_at=func.now(),
            settled_by=settled_by,
            settled_by_account_id=settled_by_account_id,
        )
        .returning(SupportEscalation)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    if orm is None:
        current = await get_request(session, escalation_id=escalation_id, tenant_id=tenant_id)
        return SettleOutcome(status="stale" if correction else "already_settled", row=current)
    left = None if available is None else max(available - (final - row.counted), Decimal(0))
    return SettleOutcome(
        status=status, row=SupportEscalationRow.model_validate(orm), available=left
    )
