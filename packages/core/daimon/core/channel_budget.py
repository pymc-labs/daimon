"""Channel budgets: which debits a budget counts, and the admission gate that reads them.

A budget caps what one channel may spend. Spend is the channel's ledger
debits (markup included) inside the budget's window; the gate refuses a new
turn once spend has reached the limit, so a limit of 0 stops the channel.
It runs after the balance and per-person cap gates, and only where a turn has
a channel. No budget row means no gate. Like the other gates it is checked
once before a turn, so a running turn can finish past the limit.

The period and validation helpers are pure; `get_channel_budget_status` and
`is_over_channel_budget` are the thin DB shell around them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import get_args

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Decision, Place, Subject, authorize
from daimon.core.errors import DaimonError
from daimon.core.stores.channel_budgets import get_channel_budget, list_channel_budgets
from daimon.core.stores.domain import BudgetWindow, ChannelBudgetRow
from daimon.core.stores.tenant_ledger import get_channel_spend, get_prepaid_balance
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BUDGET_WINDOWS: tuple[BudgetWindow, ...] = get_args(BudgetWindow)
_MAX_LIMIT_USD = Decimal("9999999999.99")  # Numeric(12, 2)


class ChannelBudgetError(DaimonError):
    """A budget request that cannot be stored; the message says what to fix."""


@dataclass(frozen=True)
class BudgetSpec:
    """A validated budget request: money as Decimal, bounds as aware UTC datetimes."""

    limit_usd: Decimal
    window: BudgetWindow
    starts_at: datetime | None
    ends_at: datetime | None


@dataclass(frozen=True)
class BudgetPeriod:
    """The half-open range `[since, until)` of debits a budget counts; None is unbounded."""

    since: datetime | None
    until: datetime | None


@dataclass(frozen=True)
class ChannelBudgetStatus:
    budget: ChannelBudgetRow
    spent_usd: Decimal
    is_active: bool
    """False before a `total` or `fixed` budget starts, or after a `fixed` one ends."""

    @property
    def remaining_usd(self) -> Decimal:
        return max(Decimal("0"), self.budget.limit_usd - self.spent_usd)

    @property
    def is_exceeded(self) -> bool:
        return self.is_active and self.spent_usd >= self.budget.limit_usd

    @property
    def percent_used(self) -> int:
        """Spend as a whole percentage of the limit; a limit of 0 reads as 100."""
        if self.budget.limit_usd <= 0:
            return 100
        return int(self.spent_usd / self.budget.limit_usd * 100)


def _parse_usd(value: str) -> Decimal:
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, ValueError) as exc:
        raise ChannelBudgetError(f"limit_usd must be a dollar amount, got {value!r}") from exc
    exponent = amount.as_tuple().exponent
    if not amount.is_finite() or amount < 0 or amount > _MAX_LIMIT_USD:
        raise ChannelBudgetError("limit_usd must be a nonnegative dollar amount")
    if isinstance(exponent, int) and exponent < -2:
        raise ChannelBudgetError("limit_usd must have at most two decimal places")
    return amount


def _parse_instant(value: str | None, *, name: str) -> datetime | None:
    """ISO 8601; a value without an offset is read as UTC."""
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ChannelBudgetError(f"{name} must be an ISO 8601 date or time, got {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_budget_spec(
    *, limit_usd: str, window: str, starts_at: str | None = None, ends_at: str | None = None
) -> BudgetSpec:
    """Validate a budget request from a tool or the CLI, before anything is written."""
    if window not in BUDGET_WINDOWS:
        raise ChannelBudgetError(f"window must be one of {', '.join(BUDGET_WINDOWS)}")
    amount = _parse_usd(limit_usd)
    start = _parse_instant(starts_at, name="starts_at")
    end = _parse_instant(ends_at, name="ends_at")
    if window == "fixed":
        if start is None or end is None:
            raise ChannelBudgetError("a fixed window needs both starts_at and ends_at")
        if start >= end:
            raise ChannelBudgetError("starts_at must be before ends_at")
    elif window == "total" and end is not None:
        raise ChannelBudgetError("ends_at is only for a fixed window")
    elif window == "monthly" and (start is not None or end is not None):
        raise ChannelBudgetError("a monthly window takes no starts_at or ends_at")
    return BudgetSpec(limit_usd=amount, window=window, starts_at=start, ends_at=end)


def budget_period(budget: ChannelBudgetRow, *, now: datetime) -> BudgetPeriod:
    """`monthly`: this UTC month. `total`: since `starts_at`, or ever. `fixed`: its range."""
    if budget.window == "monthly":
        return BudgetPeriod(since=datetime(now.year, now.month, 1, tzinfo=UTC), until=None)
    if budget.window == "total":
        return BudgetPeriod(since=budget.starts_at, until=None)
    return BudgetPeriod(since=budget.starts_at, until=budget.ends_at)


def is_budget_active(budget: ChannelBudgetRow, *, now: datetime) -> bool:
    """A budget gates from `starts_at`, if set, and a fixed one until `ends_at`, exclusive."""
    if budget.window == "monthly":
        return True
    if budget.starts_at is not None and now < budget.starts_at:
        return False
    return budget.ends_at is None or now < budget.ends_at


def _instant_label(value: datetime) -> str:
    if value.hour == value.minute == value.second == 0:
        return f"{value:%Y-%m-%d}"
    return f"{value:%Y-%m-%d %H:%M} UTC"


def window_label(budget: ChannelBudgetRow, *, started: bool = True) -> str:
    """The window in words, e.g. `monthly`, `since 2026-07-01`, `2026-07-01 until 2026-07-03`.

    A total window that has not `started` reads `from 2026-07-01`. A fixed
    window's end is exclusive, hence "until".
    """
    if budget.window == "monthly":
        return "monthly"
    if budget.starts_at is None:
        return "total"
    if budget.window == "total" or budget.ends_at is None:
        return f"{'since' if started else 'from'} {_instant_label(budget.starts_at)}"
    return f"{_instant_label(budget.starts_at)} until {_instant_label(budget.ends_at)}"


def describe_budget(status: ChannelBudgetStatus) -> str:
    """`$1.20 of $5.00 (monthly)`: the line every surface shows for a channel budget."""
    budget = status.budget
    label = window_label(budget, started=status.is_active)
    return f"${status.spent_usd:,.2f} of ${budget.limit_usd:,.2f} ({label})"


async def load_budget_status(
    session: AsyncSession, budget: ChannelBudgetRow, *, now: datetime
) -> ChannelBudgetStatus:
    period = budget_period(budget, now=now)
    spent = await get_channel_spend(
        session,
        tenant_id=budget.tenant_id,
        channel_id=budget.channel_id,
        since=period.since,
        until=period.until,
    )
    return ChannelBudgetStatus(
        budget=budget, spent_usd=spent, is_active=is_budget_active(budget, now=now)
    )


def rank_by_percent_used(
    statuses: list[ChannelBudgetStatus],
) -> list[ChannelBudgetStatus]:
    """Active budgets first, each group by the share of its limit spent, highest first. Pure."""
    return sorted(
        statuses,
        key=lambda s: (
            not s.is_active,
            -(s.spent_usd / s.budget.limit_usd if s.budget.limit_usd > 0 else Decimal("Infinity")),
            s.budget.channel_id,
        ),
    )


async def list_channel_budget_statuses(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str | None, now: datetime
) -> list[ChannelBudgetStatus]:
    """Every budget of the tenant (on `platform`, if given) with its spend, by
    `rank_by_percent_used`."""
    budgets = await list_channel_budgets(session, tenant_id=tenant_id)
    return rank_by_percent_used(
        [
            await load_budget_status(session, budget, now=now)
            for budget in budgets
            if platform is None or budget.platform == platform
        ]
    )


async def get_channel_budget_status(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    now: datetime,
) -> ChannelBudgetStatus | None:
    """The channel's budget with its spend so far, or None when it has no budget."""
    budget = await get_channel_budget(
        session, tenant_id=tenant_id, platform=platform, channel_id=channel_id
    )
    if budget is None:
        return None
    return await load_budget_status(session, budget, now=now)


async def is_over_channel_budget(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str | None,
    now: datetime,
    session: AsyncSession | None = None,
) -> bool:
    """True iff the channel has an active budget whose spend has reached its limit.

    `channel_id=None` (an older DM, an MCP call, a routine with no channel) is never
    gated. Exceptions propagate: admission fails closed.
    """
    if channel_id is None:
        return False
    if session is None:
        async with sessionmaker() as opened:
            status = await get_channel_budget_status(
                opened, tenant_id=tenant_id, platform=platform, channel_id=channel_id, now=now
            )
    else:
        status = await get_channel_budget_status(
            session, tenant_id=tenant_id, platform=platform, channel_id=channel_id, now=now
        )
    return status is not None and status.is_exceeded


async def balance_footer(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    budget_channel_id: str | None,
    now: datetime,
) -> str | None:
    """A turn footer's money: what an active channel budget has left, else the prepaid
    balance, as ``$8.10 left``; None for an operator-funded tenant outside a budgeted
    channel."""
    if budget_channel_id is not None:
        status = await get_channel_budget_status(
            session, tenant_id=tenant_id, platform=platform, channel_id=budget_channel_id, now=now
        )
        if status is not None and status.is_active:
            return f"${status.remaining_usd:.2f} left"
    balance = await get_prepaid_balance(session, tenant_id=tenant_id)
    return None if balance is None else f"${balance:.2f} left"


def may_set_channel_budget(subject: Subject, channel_id: str) -> Decision:
    """Whether ``subject`` may set, clear or raise ``channel_id``'s budget. Pure.

    No access policy fact counts, so the empty policy stands in for it.
    """
    return authorize(
        TenantAccessPolicy(),
        subject=subject,
        action=Action.SET_CHANNEL_BUDGET,
        place=Place(channel_id=channel_id),
    )
