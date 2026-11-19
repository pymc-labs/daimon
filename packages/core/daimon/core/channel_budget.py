"""Channel budgets: which debits a budget counts, and the admission gate that reads them.

A budget caps what one channel may spend. Spend is the channel's ledger
debits (markup included) inside the budget's window; the gate refuses a new
turn once spend has reached the limit, so a limit of 0 stops the channel.
An `alert` budget never refuses: it only posts alerts (`channel_budget_alert`).
It runs after the balance and per-person cap gates, and only where a turn has
a channel. No budget row means no gate. Like the other gates it is checked
once before a turn, so a running turn can finish past the limit.

The period and validation helpers are pure; `get_channel_budget_status` and
`is_over_channel_budget` are the thin DB shell around them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Literal, get_args

import structlog
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Decision, Place, Subject, authorize
from daimon.core.errors import DaimonError
from daimon.core.stores.channel_budgets import get_channel_budget, list_channel_budgets
from daimon.core.stores.domain import BudgetOnLimit, BudgetWindow, ChannelBudgetRow
from daimon.core.stores.tenant_ledger import get_channel_spend, get_prepaid_balance
from daimon.core.windows import (
    WINDOWS,
    WindowError,
    WindowPeriod,
    is_window_active,
    parse_window,
    window_label,
    window_period,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

type UsageState = Literal["none", "pending", "settled"]

log = structlog.get_logger()

BUDGET_WINDOWS: tuple[BudgetWindow, ...] = WINDOWS
BUDGET_ON_LIMIT: tuple[BudgetOnLimit, ...] = get_args(BudgetOnLimit)
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
    on_limit: BudgetOnLimit = "stop"
    alert_channel_id: str | None = None
    """Where alerts go; None is the workspace's support destination."""


BudgetPeriod = WindowPeriod


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
        """True when new turns are refused: only a `stop` budget ever refuses."""
        return (
            self.is_active
            and self.budget.on_limit == "stop"
            and self.spent_usd >= self.budget.limit_usd
        )

    @property
    def alert_level(self) -> int:
        """The full multiple of the limit spent so far (2 at twice the limit) that an `alert`
        budget alerts on; 0 below the limit and for a `stop` or inactive budget. A limit of 0
        alerts once, on the first spend."""
        budget = self.budget
        if not self.is_active or budget.on_limit != "alert":
            return 0
        if budget.limit_usd <= 0:
            return int(self.spent_usd > 0)
        return int(self.spent_usd // budget.limit_usd)

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


def parse_budget_spec(
    *,
    limit_usd: str,
    window: str,
    starts_at: str | None = None,
    ends_at: str | None = None,
    on_limit: str | None = "stop",
    alert_channel_id: str | None = None,
    current: ChannelBudgetRow | None = None,
) -> BudgetSpec:
    """Validate a budget request from a tool or the CLI, before anything is written.

    The caller has checked `alert_channel_id` is a channel Daimon can post in, in the workspace.
    `on_limit` None keeps `current`'s mode and alert channel, so editing only the limit never
    turns an alert budget into a stop one; a new budget stops.
    """
    if window not in BUDGET_WINDOWS:
        raise ChannelBudgetError(f"window must be one of {', '.join(BUDGET_WINDOWS)}")
    if on_limit is None:
        on_limit = "stop" if current is None else current.on_limit
        if alert_channel_id is None and current is not None:
            alert_channel_id = current.alert_channel_id
    mode: BudgetOnLimit = "alert" if on_limit == "alert" else "stop"
    if on_limit != mode:
        raise ChannelBudgetError(f"on_limit must be one of {', '.join(BUDGET_ON_LIMIT)}")
    if alert_channel_id is not None and mode != "alert":
        raise ChannelBudgetError("alert_channel needs on_limit alert")
    amount = _parse_usd(limit_usd)
    try:
        bounds = parse_window(window, starts_at=starts_at, ends_at=ends_at)
    except WindowError as exc:
        raise ChannelBudgetError(str(exc)) from exc
    return BudgetSpec(
        limit_usd=amount,
        window=bounds.window,
        starts_at=bounds.starts_at,
        ends_at=bounds.ends_at,
        on_limit=mode,
        alert_channel_id=alert_channel_id,
    )


# The window rules live in `daimon.core.windows`, shared with support plans.
budget_period = window_period
is_budget_active = is_window_active


def describe_budget(status: ChannelBudgetStatus) -> str:
    """`$1.20 of $5.00 (monthly)`: the line every surface shows for a channel budget."""
    budget = status.budget
    label = window_label(budget, started=status.is_active)
    if budget.on_limit == "alert":
        label += ", alert only"
    return f"${status.spent_usd:,.2f} of ${budget.limit_usd:,.2f} ({label})"


async def load_budget_status(
    session: AsyncSession, budget: ChannelBudgetRow, *, now: datetime, persisted: bool = False
) -> ChannelBudgetStatus:
    period = budget_period(budget, now=now)
    reader = partial(get_channel_spend, persisted=True) if persisted else get_channel_spend
    spent = await reader(
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
    persisted: bool = False,
) -> ChannelBudgetStatus | None:
    """The channel's budget with its spend so far, or None when it has no budget."""
    reader = partial(get_channel_budget, persisted=True) if persisted else get_channel_budget
    budget = await reader(session, tenant_id=tenant_id, platform=platform, channel_id=channel_id)
    if budget is None:
        return None
    loader = partial(load_budget_status, persisted=True) if persisted else load_budget_status
    return await loader(session, budget, now=now)


async def is_over_channel_budget(
    *,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
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
        if sessionmaker is None:
            raise ValueError("is_over_channel_budget needs a session or a sessionmaker")
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
    channel. A prepaid balance at or below zero reads ``No credit left``: a turn can
    settle a few cents past zero, and ``$-0.01 left`` is not something to show people."""
    if budget_channel_id is not None:
        status = await get_channel_budget_status(
            session, tenant_id=tenant_id, platform=platform, channel_id=budget_channel_id, now=now
        )
        # An alert-only budget never runs out, so it has nothing left to show.
        if status is not None and status.is_active and status.budget.on_limit == "stop":
            return f"${status.remaining_usd:.2f} left"
    balance = await get_prepaid_balance(session, tenant_id=tenant_id)
    if balance is None:
        return None
    return f"${balance:.2f} left" if balance > 0 else "No credit left"


def may_set_channel_budget(
    policy: TenantAccessPolicy, subject: Subject, channel_id: str
) -> Decision:
    """Whether ``subject`` may set, clear or raise ``channel_id``'s budget. Pure.

    A private channel's budget changes only from inside it (`sealed_outside`).
    """
    return authorize(
        policy,
        subject=subject,
        action=Action.SET_CHANNEL_BUDGET,
        place=Place(channel_id=channel_id),
    )
