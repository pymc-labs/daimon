"""What the admin surfaces (CLI, MCP, web) read about support plans, and how they word a settle.

One read for every surface: each plan with its menu key, what its pool spent in the
window holding now, its state and the people with their own cap under it. Read per
call (decision 9), never cached.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from daimon.core.stores import support_escalation as ledger
from daimon.core.stores import support_plans as store
from daimon.core.stores.support_rows import SupportAllowanceRow, SupportPlanRow
from daimon.core.support_plan_changes import PlanValue, read_plan
from daimon.core.support_plans import (
    PLAN_GONE,
    PlanScope,
    PlanSpec,
    SupportCatalogSpec,
    SupportPlanError,
    already_settled_text,
    credits_text,
    format_credits,
    instant_text,
    parse_credits,
    parse_plan_spec,
    parse_scope,
    price_text,
    settle_range_text,
    settled_text,
)
from daimon.core.windows import WINDOWS, Windowed, WindowState, window_state
from sqlalchemy.ext.asyncio import AsyncSession

NO_REQUEST = "No such request."
CLEAR = "none"
"""What `plan set` takes to clear a pool, a per-person cap or a date."""
_NO_POOL_USER = ""
"""`plan_spent` sums the pool for any id; no request is ever made under the empty one."""


@dataclass(frozen=True)
class PlanUsage:
    """A plan as an admin lists it."""

    plan: SupportPlanRow
    value: PlanValue
    pool_used: Decimal
    """Spent (or held) from the pool in the window holding the read's `now`."""
    state: WindowState
    allowances: tuple[SupportAllowanceRow, ...]
    summary: str
    """`1.5 of 10 credits used, 2 per person, this month`; outside its window `Ended: ...`."""


def window_text(plan: Windowed, *, now: datetime) -> str:
    """`this month`, `all time`, `since 18 Nov 09:00 UTC` or `18 Nov 00:00 UTC until ...`."""
    if plan.window == "monthly":
        return "this month"
    if plan.window == "fixed" and plan.starts_at is not None and plan.ends_at is not None:
        start, end = instant_text(plan.starts_at, now=now), instant_text(plan.ends_at, now=now)
        return f"{start} until {end}"
    if plan.starts_at is not None:
        return f"since {instant_text(plan.starts_at, now=now)}"
    return "all time"


def usage_text(plan: SupportPlanRow, *, pool_used: Decimal, now: datetime) -> str:
    parts: list[str] = []
    if plan.pool is not None:
        parts.append(f"{format_credits(pool_used)} of {credits_text(plan.pool)} used")
    if plan.per_person is not None:
        parts.append(f"{format_credits(plan.per_person)} per person")
    parts.append(window_text(plan, now=now))
    text = ", ".join(parts)
    state = window_state(plan, now=now)
    if state == "active":
        return text
    return f"{'Ended' if state == 'ended' else 'Not started'}: {text}"


async def _usage(
    session: AsyncSession, plan: SupportPlanRow, *, catalog: str | None, now: datetime
) -> PlanUsage:
    pool_used, _ = await ledger.plan_spent(
        session, plan=plan, platform_user_id=_NO_POOL_USER, as_of=now
    )
    allowances = await store.list_allowances(session, tenant_id=plan.tenant_id, plan_id=plan.id)
    value = PlanValue(
        plan.scope,
        plan.scope_id,
        catalog,
        plan.pool,
        plan.per_person,
        plan.window,
        plan.starts_at,
        plan.ends_at,
    )
    return PlanUsage(
        plan=plan,
        value=value,
        pool_used=pool_used,
        state=window_state(plan, now=now),
        allowances=tuple(allowances),
        summary=usage_text(plan, pool_used=pool_used, now=now),
    )


async def list_plan_usage(
    session: AsyncSession, *, tenant_id: uuid.UUID, now: datetime
) -> list[PlanUsage]:
    """Every plan of the tenant: workspace, then agents, then channels."""
    keys = {c.id: c.key for c in await store.list_catalogs(session, tenant_id=tenant_id)}
    plans = await store.list_plans(session, tenant_id=tenant_id)
    order: dict[str, int] = {"workspace": 0, "agent": 1, "channel": 2}
    plans.sort(key=lambda p: (order[p.scope], p.scope_id))
    return [
        await _usage(session, p, catalog=keys.get(p.catalog_id) if p.catalog_id else None, now=now)
        for p in plans
    ]


async def get_plan_usage(
    session: AsyncSession, *, tenant_id: uuid.UUID, scope: PlanScope, scope_id: str, now: datetime
) -> PlanUsage | None:
    plan = await store.get_plan(session, tenant_id=tenant_id, scope=scope, scope_id=scope_id)
    if plan is None:
        return None
    catalog = None
    if plan.catalog_id is not None:
        row = await store.get_catalog_by_id(
            session, tenant_id=tenant_id, catalog_id=plan.catalog_id
        )
        catalog = None if row is None else row.key
    return await _usage(session, plan, catalog=catalog, now=now)


def _when(value: object, *, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SupportPlanError(f"{name} must be an ISO date")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise SupportPlanError(f"{name} must be an ISO date") from exc


def parse_plan_value(data: Mapping[str, object]) -> PlanValue:
    """A plan a caller last read, as `PlanValue.as_json` wrote it (the `Expected` of a change)."""
    raw_scope, raw_id = data.get("scope"), data.get("scope_id")
    scope_id = raw_id if isinstance(raw_id, str) and raw_id else None
    scope, scope_id_ = parse_scope(raw_scope if isinstance(raw_scope, str) else "", scope_id)
    window = data.get("window")
    if window not in WINDOWS:
        raise SupportPlanError("window must be total, monthly or fixed")
    catalog = data.get("catalog")
    if catalog is not None and not isinstance(catalog, str):
        raise SupportPlanError("catalog must be a menu key")

    def credits(name: str) -> Decimal | None:
        value = data.get(name)
        return None if value is None else parse_credits(value, name=name)

    return PlanValue(
        scope,
        scope_id_,
        catalog,
        credits("pool"),
        credits("per_person"),
        window,
        _when(data.get("starts_at"), name="starts_at"),
        _when(data.get("ends_at"), name="ends_at"),
    )


@dataclass(frozen=True)
class PlanChange:
    """`plan set` options as given (CLI, MCP): None keeps the plan's value, `none` clears it."""

    catalog: str | None = None
    """A menu key or `default`."""
    pool: str | None = None
    per_person: str | None = None
    window: str | None = None
    starts_at: str | None = None
    ends_at: str | None = None


def merge_plan(
    current: PlanValue | None, *, scope: PlanScope, scope_id: str, change: PlanChange
) -> PlanSpec:
    """The plan a partial `plan set` asks for; with no plan, an option left out is unset.

    A new window keeps only the dates it can hold: none for monthly, no end for total.
    """

    def pick[T](raw: str | None, kept: T | None) -> str | T | None:
        if raw is None:
            return kept
        return None if raw.strip().lower() == CLEAR else raw

    def date(raw: str | None, kept: datetime | None) -> str | None:
        value = pick(raw, kept)
        return value.isoformat() if isinstance(value, datetime) else value

    window = change.window or (current.window if current else "total")
    start = current.starts_at if current and window != "monthly" else None
    end = current.ends_at if current and window == "fixed" else None
    catalog = current.catalog if current and change.catalog is None else change.catalog
    return parse_plan_spec(
        scope=scope,
        scope_id=scope_id or None,
        catalog=catalog,
        pool=pick(change.pool, current.pool if current else None),
        per_person=pick(change.per_person, current.per_person if current else None),
        window=window,
        starts_at=date(change.starts_at, start),
        ends_at=date(change.ends_at, end),
    )


async def merge_plan_change(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    scope: PlanScope,
    scope_id: str,
    change: PlanChange,
) -> tuple[PlanSpec, PlanValue | None]:
    """`merge_plan` over the plan read now; set it with that read as `Expected`."""
    current = await read_plan(session, tenant_id=tenant_id, scope=scope, scope_id=scope_id)
    return merge_plan(current, scope=scope, scope_id=scope_id, change=change), current


def options_text(spec: SupportCatalogSpec) -> str:
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
    if outcome.status == "plan_gone":
        return PLAN_GONE
    return "Changed meanwhile. Check the request and try again."


__all__ = [
    "CLEAR",
    "NO_REQUEST",
    "PlanChange",
    "PlanUsage",
    "get_plan_usage",
    "list_plan_usage",
    "merge_plan",
    "merge_plan_change",
    "options_text",
    "parse_plan_value",
    "settle_outcome_text",
    "usage_text",
    "window_text",
]
