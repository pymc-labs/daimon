"""Async store for support_escalations — human-support requests and their credit gate.

No try/except anywhere in this module — exceptions propagate to the adapter
boundary. Callers own the transaction; every write ends with `await
session.flush()`.

The credit gate counts inside the caller's transaction, because a check performed before the
transaction is a TOCTOU: two fast clicks both read `used=2` against an allowance of 3, both
decide they are permitted, and both insert. `submit_request` takes the person's transaction-scoped
advisory lock before it counts, so a second request for the same person waits until the first
commits or rolls back and then counts its row: the count and the insert are atomic under
`READ COMMITTED` too. It also refuses a second request from one person on one message and option,
so a double click or a platform retry neither spends a second credit nor posts twice.

Support limits (`daimon.core.support`) make the rows a credit ledger:
each is charged to the `default` ledger, a `limit`, or nothing (`exempt`), and
spends `COALESCE(charge, hold)`. `submit_request` and `settle_request` are the
limit-aware writers. Lock order, each transaction-scoped and never reversed:
the limit (`daimon:support_limit:<id>`), the person (above), then the tenant
(`lock_access_policy`). Config writers take only the tenant lock and settles
only the first two, so no two paths wait on each other in opposite orders.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal, cast

from daimon.core._models import SupportEscalation
from daimon.core.stores.access_policy import lock_access_policy
from daimon.core.stores.support import (
    get_limit_by_id,
    limits_for_place,
    menu_for_place,
)
from daimon.core.stores.support_rows import LimitView, SupportEscalationRow, SupportLimitRow
from daimon.core.support import (
    DEFAULT_OPTION_KEY,
    Balance,
    RequestChoice,
    RequestStatus,
    SettleProblem,
    governing_limit,
    hold_for,
    limit_period,
    limit_state,
    may_submit,
    offered_option,
    request_note,
    settle_problem,
    snapshot_option,
    validate_answers,
)
from daimon.core.support import SupportPlace as SupportPlace
from daimon.core.windows import WindowPeriod, WindowState
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
    true,
    tuple_,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

# Namespace prefix for the pg_advisory_xact_lock key, so this ledger's keys
# never collide with another advisory-lock user in the same database.
_LOCK_NAMESPACE = "daimon:support_escalation:"


_LIMIT_LOCK_NAMESPACE = "daimon:support_limit:"
PURGED_USER: Final = "purged"
"""The requester of a limit row whose person was erased; it keeps its charge."""
_COUNTED = func.coalesce(SupportEscalation.charge, SupportEscalation.hold)
_IS_LIMIT = SupportEscalation.charged_to == "limit"
_NOT_LIMIT = SupportEscalation.charged_to != "limit"
_ANONYMOUS: Final[dict[str, Any]] = {
    "platform_user_id": PURGED_USER,
    "account_id": None,
    "ma_session_id": None,
    "message_id": "",  # still points at their conversation; the support post id stays for settle
    "note": "",
    "answers": None,
}


async def _advisory_lock(session: AsyncSession, key: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
    )


async def _lock_limit(session: AsyncSession, *, limit_id: uuid.UUID) -> None:
    """Serialize every charge against one limit's shared credits until the transaction ends."""
    await _advisory_lock(session, f"{_LIMIT_LOCK_NAMESPACE}{limit_id}")


async def lock_limit(session: AsyncSession, *, limit_id: uuid.UUID) -> None:
    """Take a limit's charge lock. It comes before the tenant lock, as in `submit_request`."""
    await _lock_limit(session, limit_id=limit_id)


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
    Limit and exempt rows never count here.
    """
    stmt = select(func.coalesce(func.sum(_COUNTED), 0)).where(
        SupportEscalation.tenant_id == tenant_id,
        SupportEscalation.platform_user_id == platform_user_id,
        SupportEscalation.charged_to == "default",
    )
    return Decimal((await session.execute(stmt)).scalar_one())


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
    """Delete one platform user's escalations in one tenant, but for limit rows. Idempotent.

    Tenant-scoped for the same reason the message_feedback counterpart is:
    `platform_user_id` is not globally unique across platforms, and purging
    ONE principal must not erase the same person's rows in another install.
    """
    result = await session.execute(
        delete(SupportEscalation).where(
            SupportEscalation.tenant_id == tenant_id,
            SupportEscalation.platform_user_id == platform_user_id,
            _NOT_LIMIT,
        )
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def anonymize_limit_requests_for_platform_user(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform_user_id: str
) -> int:
    """Erase the person from their limit rows and keep the charges. Idempotent.

    Shared credits are not refunded by one member leaving. The option snapshot
    is the tenant's menu, not the person's words, so it stays.
    """
    result = await session.execute(
        update(SupportEscalation)
        .where(
            SupportEscalation.tenant_id == tenant_id,
            SupportEscalation.platform_user_id == platform_user_id,
            _IS_LIMIT,
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
    """Delete every escalation attributable to `account_id`, but for limit rows. Idempotent."""
    predicate = _account_or_platform_user_predicate(
        account_id=account_id, platform_user_keys=platform_user_keys
    )
    result = await session.execute(delete(SupportEscalation).where(predicate, _NOT_LIMIT))
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
    stmt = select(func.count()).select_from(SupportEscalation).where(predicate, _NOT_LIMIT)
    return int((await session.execute(stmt)).scalar_one())


async def anonymize_limit_requests_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    platform_user_keys: Sequence[tuple[uuid.UUID, str]],
) -> int:
    """`anonymize_limit_requests_for_platform_user` over the account predicate. Idempotent."""
    predicate = _account_or_platform_user_predicate(
        account_id=account_id, platform_user_keys=platform_user_keys
    )
    stmt = update(SupportEscalation).where(predicate, _IS_LIMIT).values(**_ANONYMOUS)
    result = await session.execute(stmt)
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def count_limit_requests_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    platform_user_keys: Sequence[tuple[uuid.UUID, str]],
) -> int:
    """Count what `anonymize_limit_requests_for_account` would anonymize. Read-only."""
    predicate = _account_or_platform_user_predicate(
        account_id=account_id, platform_user_keys=platform_user_keys
    )
    stmt = select(func.count()).select_from(SupportEscalation).where(predicate, _IS_LIMIT)
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


# ---------- Limits: balances, submit, settle ----------


async def limit_spent(
    session: AsyncSession,
    *,
    limit: SupportLimitRow,
    platform_user_id: str,
    as_of: datetime,
    person_period: WindowPeriod | None = None,
) -> tuple[Decimal, Decimal]:
    """(the shared spend, this person's spend) on one limit's ledger in its period at `as_of`.

    `person_period` is the period of the person's own limit, when they have one.
    """
    period = limit_period(limit, as_of=as_of)
    person_period = person_period or period
    base: list[ColumnElement[bool]] = [
        SupportEscalation.tenant_id == limit.tenant_id,
        SupportEscalation.limit_id == limit.id,
        _IS_LIMIT,
    ]
    person = func.sum(_COUNTED).filter(
        SupportEscalation.platform_user_id == platform_user_id, *_within(person_period)
    )
    shared = func.sum(_COUNTED).filter(*_within(period))
    stmt = select(func.coalesce(shared, 0), func.coalesce(person, 0)).where(*base)
    shared_used, person_used = (await session.execute(stmt)).one()
    return Decimal(shared_used), Decimal(person_used)


def _within(period: WindowPeriod) -> list[ColumnElement[bool]]:
    where: list[ColumnElement[bool]] = [true()]
    if period.since is not None:
        where.append(SupportEscalation.created_at >= period.since)
    if period.until is not None:
        where.append(SupportEscalation.created_at < period.until)
    return where


async def _default_spent_in(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform_user_id: str, period: WindowPeriod
) -> Decimal:
    stmt = select(func.coalesce(func.sum(_COUNTED), 0)).where(
        SupportEscalation.tenant_id == tenant_id,
        SupportEscalation.platform_user_id == platform_user_id,
        SupportEscalation.charged_to == "default",
        *_within(period),
    )
    return Decimal((await session.execute(stmt)).scalar_one())


async def _balance(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    limit: SupportLimitRow | None,
    person: SupportLimitRow | None,
    platform_user_id: str,
    default_allowance: int,
    as_of: datetime,
) -> Balance:
    """Both axes for one person; their own limit replaces the cap and its period."""
    person_period = None if person is None else limit_period(person, as_of=as_of)
    if limit is None:
        if person is None:
            used = await default_spent(
                session, tenant_id=tenant_id, platform_user_id=platform_user_id
            )
            cap = Decimal(default_allowance)
        else:
            assert person_period is not None
            used = await _default_spent_in(
                session,
                tenant_id=tenant_id,
                platform_user_id=platform_user_id,
                period=person_period,
            )
            cap = person.each_person
        return Balance(shared=None, shared_used=Decimal(0), cap=cap, person_used=used)
    cap = limit.each_person if person is None else person.each_person
    if platform_user_id == PURGED_USER:
        cap = None  # erased people share one id; only the shared credits still bind
    shared_used, person_used = await limit_spent(
        session,
        limit=limit,
        platform_user_id=platform_user_id,
        as_of=as_of,
        person_period=person_period,
    )
    return Balance(shared=limit.shared, shared_used=shared_used, cap=cap, person_used=person_used)


def _state(*rows: SupportLimitRow | None, now: datetime) -> WindowState:
    for row in rows:
        if row is not None and (state := limit_state(row, now=now)) != "active":
            return state
    return "active"


@dataclass(frozen=True)
class _Governing:
    limit: SupportLimitRow | None
    person: SupportLimitRow | None


async def _governing(
    session: AsyncSession, *, tenant_id: uuid.UUID, place: SupportPlace, platform_user_id: str
) -> _Governing:
    rows = await limits_for_place(
        session, tenant_id=tenant_id, place=place, platform_user_id=platform_user_id
    )
    limit = governing_limit([r for r in rows if r.platform_user_id is None], place)
    person = governing_limit([r for r in rows if r.platform_user_id is not None], place)
    return _Governing(limit=limit, person=person)


async def _view(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    place: SupportPlace,
    governing: _Governing,
    platform_user_id: str,
    default_allowance: int,
    exempt: bool,
    now: datetime,
) -> LimitView:
    menu = await menu_for_place(session, tenant_id=tenant_id, place=place)
    balance = await _balance(
        session,
        tenant_id=tenant_id,
        limit=governing.limit,
        person=governing.person,
        platform_user_id=platform_user_id,
        default_allowance=default_allowance,
        as_of=now,
    )
    return LimitView(
        limit=governing.limit,
        person=governing.person,
        menu=menu,
        balance=balance,
        state=_state(governing.limit, governing.person, now=now),
        exempt=exempt,
    )


async def resolve_limit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    place: SupportPlace,
    platform_user_id: str,
    default_allowance: int,
    exempt: bool = False,
    now: datetime | None = None,
) -> LimitView:
    """The limit, menu and balance for one person in one place, read now, never cached.

    `default_allowance` is `settings.support.credits_per_user`. `exempt` is the
    adapter's native-admin check for the requester.
    """
    governing = await _governing(
        session, tenant_id=tenant_id, place=place, platform_user_id=platform_user_id
    )
    return await _view(
        session,
        tenant_id=tenant_id,
        place=place,
        governing=governing,
        platform_user_id=platform_user_id,
        default_allowance=default_allowance,
        exempt=exempt,
        now=now or datetime.now(UTC),
    )


@dataclass(frozen=True)
class PersonStanding:
    """One person limit as it stands: what it spent and the requests on that ledger."""

    balance: Balance
    recent: list[SupportEscalationRow]


async def person_standing(
    session: AsyncSession,
    *,
    person: SupportLimitRow,
    default_allowance: int,
    now: datetime | None = None,
    recent: int = 50,
) -> PersonStanding:
    """A person limit's balance and newest requests, as a request at its place would see them.

    The place is the limit's own (everywhere, that agent or that channel). The person's
    spend counts on the ledger that place charges (the governing limit's, or the default
    one when it has no limit), through the same read `submit_request` charges by.
    """
    user = person.platform_user_id
    assert user is not None, "a place limit has no person"
    place = SupportPlace(
        channel_id=person.scope_id if person.scope == "channel" else None,
        agent_id=person.scope_id if person.scope == "agent" else None,
    )
    at = now or datetime.now(UTC)
    governing = await _governing(
        session, tenant_id=person.tenant_id, place=place, platform_user_id=user
    )
    balance = await _balance(
        session,
        tenant_id=person.tenant_id,
        limit=governing.limit,
        person=person,
        platform_user_id=user,
        default_allowance=default_allowance,
        as_of=at,
    )
    on_ledger = (
        SupportEscalation.limit_id == governing.limit.id
        if governing.limit is not None
        else SupportEscalation.charged_to == "default"
    )
    stmt = (
        select(SupportEscalation)
        .where(
            SupportEscalation.tenant_id == person.tenant_id,
            SupportEscalation.platform_user_id == user,
            on_ledger,
        )
        .order_by(SupportEscalation.created_at.desc())
        .limit(recent)
    )
    rows = [SupportEscalationRow.model_validate(o) for o in (await session.scalars(stmt)).all()]
    return PersonStanding(balance=balance, recent=rows)


SubmitStatus = Literal[
    "recorded", "duplicate", "out_of_credits", "stale", "refused", "limit_inactive"
]


@dataclass(frozen=True)
class SubmitOutcome:
    """What `submit_request` did.

    `row` is the new row for "recorded" and the person's earlier one for
    "duplicate". `view` is the limit read under the locks, with the new row
    spent; None for "refused", where nothing was read.
    """

    status: SubmitStatus
    row: SupportEscalationRow | None
    view: LimitView | None


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
    support_channel_id: str | None = None,
    now: datetime | None = None,
) -> SubmitOutcome:
    """Record one request on the governing limit, if its terms still stand and it is affordable.

    Locks the limit (unless `exempt`, which spends nothing), the person and the
    tenant, then re-reads everything under them, so the balance, the menu and
    the duplicate check are atomic with the insert. "stale" means the limit, or
    the picked option's price or fields, changed since it was shown
    (`OPTIONS_CHANGED`); a label-only change records the option as it is now. A bad step or
    answer raises `SupportError`; adapters check them on the menu first.
    `source_allowed` is the caller's authoritative access decision for the place the request
    comes from. It runs inside this transaction after the locks are held, so it must read the
    policy itself (fresh): a policy edit committed while this waited is read and refuses, with
    nothing spent. Lock order is the limit, the person, then the tenant (`lock_access_policy`).
    `support_channel_id` is where it is sent, kept so later menu edits never move it.
    Only an exempt request settles here; every other one waits for a person to accept,
    settle or decline it.
    The policy writers and a private form's consume never take a ledger lock, so it cannot
    deadlock.
    """
    now = now or datetime.now(UTC)
    shown = await _governing(
        session, tenant_id=tenant_id, place=place, platform_user_id=platform_user_id
    )
    if shown.limit is not None and not exempt:
        await _lock_limit(session, limit_id=shown.limit.id)
    await _lock_user_ledger(session, tenant_id=tenant_id, platform_user_id=platform_user_id)
    await lock_access_policy(session, tenant_id=tenant_id)
    if source_allowed is not None and not await source_allowed(session):
        return SubmitOutcome(status="refused", row=None, view=None)
    governing = await _governing(
        session, tenant_id=tenant_id, place=place, platform_user_id=platform_user_id
    )
    view = await _view(
        session,
        tenant_id=tenant_id,
        place=place,
        governing=governing,
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
    same_limit = _limit_key(governing.limit) == _limit_key(shown.limit)
    if not same_limit or option is None:
        return SubmitOutcome(status="stale", row=None, view=view)
    snapshot = snapshot_option(option, step=choice.step)
    answers = validate_answers(option, choice.answers)
    if not view.active:
        return SubmitOutcome(status="limit_inactive", row=None, view=view)
    bounds = snapshot.bounds
    if not exempt and not may_submit(bounds, view.available):
        return SubmitOutcome(status="out_of_credits", row=None, view=view)
    hold = Decimal(0) if exempt else hold_for(bounds)
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
            # The clock the balance and period checks read: the DB's transaction time could
            # fall in the next month, or before `starts_at`, and count in a different period.
            created_at=now,
            note=request_note(option, answers),
            charged_to=view.charged_to,
            limit_id=_limit_key(governing.limit),
            person_limit_id=_limit_key(governing.person),
            option_key=option.key,
            option=snapshot.model_dump(mode="json", exclude_none=True),
            answers=answers,
            hold=hold,
            charge=hold if exempt else None,
            status="settled" if exempt else "pending",
            support_channel_id=support_channel_id,
        )
        .returning(SupportEscalation)
    )
    row = SupportEscalationRow.model_validate((await session.execute(stmt)).scalar_one())
    await session.flush()
    if not exempt:
        balance = replace(
            view.balance,
            shared_used=view.balance.shared_used + hold,
            person_used=view.balance.person_used + hold,
        )
        view = replace(view, balance=balance)
    return SubmitOutcome(status="recorded", row=row, view=view)


def _limit_key(limit: SupportLimitRow | None) -> uuid.UUID | None:
    return None if limit is None else limit.id


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
    limit_id: uuid.UUID | None = None,
    undelivered: bool = False,
    limit: int = 50,
) -> list[SupportEscalationRow]:
    """Newest first; `undelivered` keeps the requests no support channel received and no person
    handled: pending ones, and ones charged at submit (`awaiting_person`).

    A request a person settled or declined needs nobody, so it never counts as lost."""
    stmt = select(SupportEscalation).where(SupportEscalation.tenant_id == tenant_id)
    if status is not None:
        stmt = stmt.where(SupportEscalation.status == status)
    if undelivered:
        stmt = stmt.where(
            SupportEscalation.delivered_at.is_(None), SupportEscalation.settled_at.is_(None)
        )
    if limit_id is not None:
        stmt = stmt.where(SupportEscalation.limit_id == limit_id)
    stmt = stmt.order_by(SupportEscalation.created_at.desc()).limit(limit)
    return [SupportEscalationRow.model_validate(o) for o in (await session.scalars(stmt)).all()]


SettleStatus = Literal[
    "settled",
    "declined",
    "already_settled",
    "stale",
    "invalid",
    "over_balance",
    "limit_gone",
    "not_found",
    "not_here",
    "own_request",
]


@dataclass(frozen=True)
class SettleOutcome:
    """What `settle_request` did; `row` is the request as it now stands (None if not found).

    `available` is the balance left on the row's ledger with the row's current
    charge spent, None when unbounded or not read (a decline, an orphan).
    "invalid" and "over_balance" pair with `settle_range_text`. "limit_gone" is a
    charge on a request whose limit was removed: it has no balance to check, so
    only a decline (or a correction down) goes through.
    """

    status: SettleStatus
    row: SupportEscalationRow | None
    available: Decimal | None = None


async def _settle_person(
    session: AsyncSession, *, row: SupportEscalationRow
) -> SupportLimitRow | None:
    """The person's own limit that capped the request at submit, as it stands now."""
    if row.person_limit_id is None:
        return None
    return await get_limit_by_id(session, tenant_id=row.tenant_id, limit_id=row.person_limit_id)


async def _ledger_available(
    session: AsyncSession, *, row: SupportEscalationRow, default_allowance: int
) -> Decimal | None:
    """The balance left on the row's ledger in the period it was made in; None if unbounded."""
    limit = None
    if row.charged_to == "limit":
        if row.limit_id is None:
            return None  # its limit was removed: it counts nowhere
        limit = await get_limit_by_id(session, tenant_id=row.tenant_id, limit_id=row.limit_id)
        if limit is None:
            return None
    person = await _settle_person(session, row=row)
    balance = await _balance(
        session,
        tenant_id=row.tenant_id,
        limit=limit,
        person=person,
        platform_user_id=row.platform_user_id,
        default_allowance=default_allowance,
        as_of=row.created_at,
    )
    return balance.available


async def decline_pending_for_limit(
    session: AsyncSession,
    *,
    limit_id: uuid.UUID,
    settled_by: str | None,
    settled_by_account_id: uuid.UUID | None,
) -> list[SupportEscalationRow]:
    """Decline every pending request of a limit about to be removed, at no charge.

    The caller holds the limit's lock (`lock_limit`), which keeps settles and
    new requests off it. A request left pending would settle with no balance
    to bound it once the limit row is gone.
    """
    stmt = (
        update(SupportEscalation)
        .where(SupportEscalation.limit_id == limit_id, SupportEscalation.status == "pending")
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
    accept: bool = False,
) -> SettleOutcome:
    """Settle a pending request at `charge`, or decline it (`charge` None: charged 0).

    `accept` settles at the row's `accept_charge` instead of `charge`, and is
    "invalid" for a price a person must set.

    A request charged at submit (`charged_at_submit`) takes `charge` equal to its
    charge, which keeps it and records the settler, or None, which refunds it.
    `expected_charge` re-settles a settled or declined row instead (an admin's
    refund or correction, down to 0), and is "stale" unless the row still
    charges exactly that. Locks the row's limit, then the requester, so a
    second click waits and reads "already_settled". `tenant_id` None is the
    support post's path. A settle may come after the limit ended; the
    balance is the one of the period the request was made in.
    """
    row = await get_request(session, escalation_id=escalation_id, tenant_id=tenant_id)
    if row is None:
        return SettleOutcome(status="not_found", row=None)
    if row.charged_to == "limit" and row.limit_id is not None:
        await _lock_limit(session, limit_id=row.limit_id)
    await _lock_user_ledger(session, tenant_id=row.tenant_id, platform_user_id=row.platform_user_id)
    row = await get_request(session, escalation_id=escalation_id, tenant_id=tenant_id)
    if row is None:
        return SettleOutcome(status="not_found", row=None)
    if accept and (charge := row.accept_charge) is None:
        return SettleOutcome(status="invalid", row=row)
    correction = expected_charge is not None
    if not correction and not row.awaiting_person:
        return SettleOutcome(status="already_settled", row=row)
    # An exempt request has no amount to set: a person may only keep it or decline it.
    if row.charged_to == "exempt" and not (row.charged_at_submit and not correction):
        return SettleOutcome(status="invalid", row=row)
    if correction and (row.status == "pending" or row.charge != expected_charge):
        return SettleOutcome(status="stale", row=row)
    keeping = row.charged_at_submit and not correction and charge is not None
    if keeping and charge != row.charge:
        return SettleOutcome(status="invalid", row=row)
    if (
        row.charged_to == "limit"
        and row.limit_id is None
        and charge is not None
        and not keeping
        and not (correction and charge <= (row.charge or Decimal(0)))
    ):
        return SettleOutcome(status="limit_gone", row=row)
    available = None
    if charge is not None and not keeping:
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
