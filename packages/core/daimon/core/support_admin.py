"""What the admin surfaces (CLI, MCP, web) read about support limits, and how they word a settle.

One read for every surface: each place's limit with its menu key, what its shared
credits spent in the period holding now, its state and the people with their own
limit there. Read per call, never cached.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from daimon.core.channel_changes import Expected
from daimon.core.stores import support as store
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores.support_rows import SupportLimitRow
from daimon.core.support import (
    LIMIT_GONE,
    LimitSpec,
    MenuSpec,
    SupportScope,
    already_settled_text,
    credits_text,
    format_credits,
    instant_text,
    limit_state,
    parse_limit_spec,
    parse_menu_key,
    price_text,
    settle_range_text,
    settled_text,
)
from daimon.core.windows import WindowState
from sqlalchemy.ext.asyncio import AsyncSession

NO_REQUEST = "No such request."
NOT_HERE = "Settle it from the channel it was posted to."
CLEAR = "none"
"""What `limit set` takes to clear an amount or a date."""


def expected_or_none[T](raw: str | None, parse: Callable[[str], T]) -> Expected[T | None] | None:
    """An `expected` value as every surface takes it.

    None: not checked. `none` in any case: expects no value. Else `parse(raw)`; a bad value
    raises `SupportError` or `ValueError`.
    """
    if raw is None:
        return None
    if raw.strip().lower() == CLEAR:
        return Expected(None)
    return Expected(parse(raw))


def expected_menu_key(raw: str | None) -> Expected[str | None] | None:
    """`expected_or_none` for a menu key, which is parsed like any menu key."""
    return expected_or_none(raw, parse_menu_key)


_NO_PERSON = ""
"""`limit_spent` sums the shared credits for any id; no request is made under the empty one."""


@dataclass(frozen=True)
class LimitUsage:
    """A place's limit as an admin lists it, with the people who have their own."""

    limit: SupportLimitRow
    menu: str | None
    """The key of the menu the place picked itself; None: the one above it, or none."""
    shared_used: Decimal
    """Spent (or held) from the shared credits in the period holding the read's `now`."""
    state: WindowState
    people: tuple[SupportLimitRow, ...]
    summary: str
    """`1.5 of 10 credits used, 2 each, this month`; outside its dates `Ended: ...`."""


def window_text(limit: SupportLimitRow, *, now: datetime) -> str:
    """When a limit counts: `this month`, `from 1 May to 1 Jun`, `all time`."""
    dates = ""
    if limit.starts_at is not None and limit.ends_at is not None:
        start, end = instant_text(limit.starts_at, now=now), instant_text(limit.ends_at, now=now)
        dates = f"from {start} to {end}"
    elif limit.starts_at is not None:
        dates = f"since {instant_text(limit.starts_at, now=now)}"
    elif limit.ends_at is not None:
        dates = f"until {instant_text(limit.ends_at, now=now)}"
    if limit.window == "monthly":
        return f"each month {dates}".strip()
    return dates or "all time"


def usage_text(limit: SupportLimitRow, *, shared_used: Decimal, now: datetime) -> str:
    """One line for a limit's use, or why it does not apply now."""
    parts: list[str] = []
    if limit.shared is not None:
        parts.append(f"{format_credits(shared_used)} of {credits_text(limit.shared)} used")
    if limit.each_person is not None:
        parts.append(f"{format_credits(limit.each_person)} each")
    parts.append(window_text(limit, now=now))
    text = ", ".join(parts)
    state = limit_state(limit, now=now)
    if state == "not_started":
        return f"Not started: {text}"
    if state == "ended":
        return f"Ended: {text}"
    return text


async def _usage(session: AsyncSession, limit: SupportLimitRow, *, now: datetime) -> LimitUsage:
    shared_used, _ = await ledger.limit_spent(
        session, limit=limit, platform_user_id=_NO_PERSON, as_of=now
    )
    people = [
        p
        for p in await store.list_limits(session, tenant_id=limit.tenant_id, people=True)
        if (p.scope, p.scope_id) == (limit.scope, limit.scope_id)
    ]
    menu = await _menu_key(session, limit.tenant_id, limit.scope, limit.scope_id)
    return LimitUsage(
        limit=limit,
        menu=menu,
        shared_used=shared_used,
        state=limit_state(limit, now=now),
        people=tuple(people),
        summary=usage_text(limit, shared_used=shared_used, now=now),
    )


async def _menu_key(
    session: AsyncSession, tenant_id: uuid.UUID, scope: SupportScope, scope_id: str
) -> str | None:
    use = await store.get_menu_use(session, tenant_id=tenant_id, scope=scope, scope_id=scope_id)
    if use is None:
        return None
    menu = await store.get_menu_by_id(session, tenant_id=tenant_id, menu_id=use.menu_id)
    return None if menu is None else menu.key


_ORDER = {"workspace": 0, "agent": 1, "channel": 2}


async def list_limit_usage(
    session: AsyncSession, *, tenant_id: uuid.UUID, now: datetime
) -> list[LimitUsage]:
    """Every place limit of the tenant: workspace, then agents, then channels."""
    limits = await store.list_limits(session, tenant_id=tenant_id, people=False)
    limits.sort(key=lambda p: (_ORDER[p.scope], p.scope_id))
    return [await _usage(session, p, now=now) for p in limits]


async def get_limit_usage(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    scope: SupportScope,
    scope_id: str,
    now: datetime,
) -> LimitUsage | None:
    """One place's limit, or None."""
    limit = await store.get_limit(session, tenant_id=tenant_id, scope=scope, scope_id=scope_id)
    return None if limit is None else await _usage(session, limit, now=now)


@dataclass(frozen=True)
class LimitChange:
    """`limit set` options as given (CLI, MCP): None keeps the value, `none` clears it."""

    shared: str | None = None
    each_person: str | None = None
    window: str | None = None
    starts_at: str | None = None
    ends_at: str | None = None


def merge_limit(
    current: SupportLimitRow | None,
    *,
    scope: SupportScope,
    scope_id: str,
    change: LimitChange,
    platform_user_id: str | None = None,
) -> LimitSpec:
    """The limit a partial `limit set` asks for; with no limit, an option left out is unset."""

    def pick[T](raw: str | None, kept: T | None) -> str | T | None:
        if raw is None:
            return kept
        return None if raw.strip().lower() == CLEAR else raw

    return parse_limit_spec(
        scope=scope,
        scope_id=scope_id or None,
        platform_user_id=platform_user_id,
        shared=pick(change.shared, current.shared if current else None),
        each_person=pick(change.each_person, current.each_person if current else None),
        window=change.window or (current.window if current else "total"),
        starts_at=pick(change.starts_at, current.starts_at if current else None),
        ends_at=pick(change.ends_at, current.ends_at if current else None),
    )


async def merge_limit_change(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    scope: SupportScope,
    scope_id: str,
    change: LimitChange,
    platform_user_id: str | None = None,
) -> tuple[LimitSpec, int | None]:
    """`merge_limit` over the row read now, and its revision to pass as `Expected`."""
    current = await store.get_limit(
        session,
        tenant_id=tenant_id,
        scope=scope,
        scope_id=scope_id,
        platform_user_id=platform_user_id,
    )
    spec = merge_limit(
        current, scope=scope, scope_id=scope_id, change=change, platform_user_id=platform_user_id
    )
    return spec, None if current is None else current.revision


def options_text(spec: MenuSpec) -> str:
    """A menu in one line: `Review (1-3 credits), Call (2 credits)`."""
    return ", ".join(f"{o.label} ({price_text(o.price)})" for o in spec.options)


def settle_outcome_text(outcome: ledger.SettleOutcome, *, correction: bool = False) -> str:
    """The settler's answer for every outcome; a done settle carries the decision-8 warning."""
    row = outcome.row
    if row is None:
        return NO_REQUEST
    if outcome.status == "settled":
        return settled_text(charge=row.charge)
    if outcome.status == "declined":
        return settled_text(charge=None)
    if outcome.status == "already_settled":
        return already_settled_text(status=row.status, charge=row.charge, by=row.settled_by)
    if outcome.status in ("invalid", "over_balance"):
        return settle_range_text(
            row.bounds, counted=row.counted, available=outcome.available, correction=correction
        )
    if outcome.status == "limit_gone":
        return LIMIT_GONE
    if outcome.status == "not_here":
        return NOT_HERE
    return "Changed meanwhile. Check the request and try again."


__all__ = [
    "CLEAR",
    "expected_menu_key",
    "expected_or_none",
    "NOT_HERE",
    "NO_REQUEST",
    "LimitChange",
    "LimitUsage",
    "get_limit_usage",
    "list_limit_usage",
    "merge_limit",
    "merge_limit_change",
    "options_text",
    "settle_outcome_text",
    "usage_text",
    "window_text",
]
