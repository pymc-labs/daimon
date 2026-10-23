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

import asyncio
import math
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext
from functools import partial
from typing import Literal, get_args

import structlog
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Decision, Place, Subject, Surface, authorize
from daimon.core.errors import DaimonError
from daimon.core.fleet import FleetConflict, FleetRun, FleetWorker, spawn_operation_key, worker_slot
from daimon.core.neutral_resources import (
    ResolvedWorkerSpec,
    WorkerPermissions,
    audit_worker_action,
)
from daimon.core.pricing import ModelRates, ProviderPrice
from daimon.core.stores import accounts, mux_state, tenant_ledger
from daimon.core.stores import fleet as fleet_store
from daimon.core.stores.channel_budgets import (
    check_channel_budget_read,
    get_channel_budget,
    list_channel_budgets,
)
from daimon.core.stores.domain import BudgetWindow, ChannelBudgetRow
from daimon.core.stores.tenant_ledger import get_channel_spend, get_prepaid_balance
from daimon.core.stores.turn_usage import (
    AccountingPrefetch,
    prefetch_accounting,
    read_accounting_revisions,
)
from daimon.core.usage_delegation import HostUsageBinding, read_delegated_usage
from mux.contracts.ids import Scope
from mux.contracts.resources import ProviderBinding
from mux.profiles import get_profile
from mux.state.lease import Slot
from mux.state.operations import OperationRecord, request_digest
from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

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
    channel. A prepaid balance at or below zero reads ``No credit left``: a turn can
    settle a few cents past zero, and ``$-0.01 left`` is not something to show people."""
    if budget_channel_id is not None:
        status = await get_channel_budget_status(
            session, tenant_id=tenant_id, platform=platform, channel_id=budget_channel_id, now=now
        )
        if status is not None and status.is_active:
            return f"${status.remaining_usd:.2f} left"
    balance = await get_prepaid_balance(session, tenant_id=tenant_id)
    if balance is None:
        return None
    return f"${balance:.2f} left" if balance > 0 else "No credit left"


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


@dataclass(frozen=True)
class FleetBudgetUsage:
    """Persisted financial facts, not a bearer capability for native dispatch.

    Exposure includes all unknown final usage at its whole worker reservation.
    Original ledger debit is already contained in exposure, never added twice.
    Only proven never-allocated terminal workers release their unspent hold.
    """

    run: FleetRun
    worker: FleetWorker
    worker_floor_usd: Decimal
    prepaid_available_usd: Decimal | None
    channel_available_usd: Decimal | None
    admitted: bool
    reason: str | None

    @property
    def run_exposure_usd(self) -> Decimal:
        return self.run.reserved_usd

    @property
    def worker_exposure_usd(self) -> Decimal:
        return self.worker.reserved_usd


async def _fleet_worker_money(
    session: AsyncSession,
    run: FleetRun,
    worker: FleetWorker,
    *,
    scope: Scope,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
    price_floor: bool,
    prefetch: AccountingPrefetch | None = None,
) -> tuple[Decimal, Decimal, bool, bool]:
    """One worker's (floor, debit, unpriced usage, no native evidence).

    With the graph's `prefetch` it answers from those rows, with the same checks.
    """
    slot = worker_slot(run, worker)
    binding = (
        await mux_state.get_binding(session, slot)
        if prefetch is None
        else await mux_state.cached_binding(session, prefetch.bindings, slot)
    )
    if binding is None:
        if worker.binding_id is not None:
            raise FleetConflict("accepted worker binding is unavailable")
        return Decimal(0), Decimal(0), False, True
    if (
        binding.provider != worker.backend
        or binding.profile != worker.profile
        or worker.binding_id not in (None, binding.id)
    ):
        raise FleetConflict("worker binding differs from its admitted identity")
    rows = await read_accounting_revisions(
        session, tenant_id=run.tenant_id, binding=binding, prefetch=prefetch
    )
    with localcontext() as arithmetic:
        arithmetic.prec = 80
        debit = -sum((row.delta_usd for row in rows.ledger), Decimal(0))
        if debit < 0:
            raise FleetConflict("worker ledger cannot prove nonnegative spend")
        if not rows.revisions or not price_floor:
            return debit, debit, False, False
        native = rows.revisions[0].observation.session
        # The rows just read are this binding's revisions; prefetched, they are not re-read.
        usage = await read_delegated_usage(
            session,
            scope=scope,
            bindings=(HostUsageBinding(binding, native),),
            prices=prices,
            rows=None if prefetch is None else {binding.id: rows},
        )
        floor = (usage.token_floor_usd * markup).quantize(
            Decimal("0.000001"), rounding=ROUND_CEILING
        )
        return max(floor, debit), debit, bool(usage.unpriced_observations), False


async def _prefetch_fleet_money(
    session: AsyncSession, graph: fleet_store.BudgetGraph, *, tenant_id: uuid.UUID
) -> AccountingPrefetch | None:
    """Every graph worker's binding and accounting rows in five statements, not per worker."""
    slots = [worker_slot(run, worker) for run, workers in graph for worker in workers]
    if not slots:
        return None
    return await prefetch_accounting(session, tenant_id=tenant_id, slots=slots)


def _root_money(record: OperationRecord) -> tuple[Decimal, Decimal]:
    finance, slot = record.result.get("root_finance"), record.slot
    if (
        slot is None
        or (slot.tenant_id, slot.account_id) != (record.tenant_id, record.account_id)
        or not isinstance(finance, dict)
    ):
        raise FleetConflict("root financial identity is unavailable")
    try:
        exposure, debit = (Decimal(str(finance[name])) for name in ("exposure_usd", "debit_usd"))
        if (
            any(not amount.is_finite() or amount < 0 for amount in (exposure, debit))
            or debit > exposure
        ):
            raise ValueError
    except (KeyError, InvalidOperation, ValueError):
        raise FleetConflict("root financial evidence is incomplete") from None
    return exposure, debit


async def _financial_room(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    graph: fleet_store.BudgetGraph,
    roots: tuple[OperationRecord, ...],
) -> tuple[Decimal | None, Decimal | None]:
    """One locked inventory of original root/worker exposure and ledger debits."""
    with localcontext() as arithmetic:
        arithmetic.prec = 80
        held = [(r.platform, r.channel_id, r.reserved_usd - r.observed_usd) for r, _ in graph]
        for root in roots:
            exposure, debit = _root_money(root)
            assert root.slot is not None
            channel = root.slot.thread.channel
            held.append((channel.platform, channel.channel_id, exposure - debit))
        balance = await tenant_ledger.get_prepaid_balance(
            session, tenant_id=tenant_id, persisted=True
        )
        available = (
            None
            if balance is None
            else balance - sum((amount for _, _, amount in held), Decimal(0))
        )
        budget = await get_channel_budget_status(
            session,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=channel_id,
            now=datetime.now(UTC),
            persisted=True,
        )
        channel_available = (
            None
            if budget is None or not budget.is_active
            else budget.remaining_usd
            - sum((amount for p, c, amount in held if (p, c) == (platform, channel_id)), Decimal(0))
        )
        return available, channel_available


def _check_financial_room(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    roots: tuple[OperationRecord, ...],
) -> None:
    """`_financial_room`'s refusals without its reads, for a caller that discards its money.

    Incomplete root evidence, then unflushed caller state on the balance or the channel
    budget, refuse here as they would there, before the caller writes anything. Its
    channel spend read guards only the ledger, which the balance read already guards.
    """
    for root in roots:
        _root_money(root)
    tenant_ledger.check_prepaid_balance_read(session, tenant_id=tenant_id)
    check_channel_budget_read(
        session, tenant_id=tenant_id, platform=platform, channel_id=channel_id
    )


async def require_root_funding(
    session: AsyncSession,
    scope: Scope,
    slot: Slot,
    *,
    origin_id: uuid.UUID,
    controls: Mapping[str, JsonValue],
    markup: Decimal,
    intent_id: uuid.UUID | None = None,
) -> OperationRecord:
    """Reserve or recheck the original root under the tenant financial fence.

    Origin/intent validation follows this call in the SAME caller transaction.
    Unknown allocation retains its original hold; replay never reserves a second one.
    """
    if (
        scope.is_platform
        or scope.is_legacy_host_authorized
        or slot.account_id != scope.account_id
        or slot.tenant_id != scope.tenant_id
    ):
        raise FleetConflict("root funding requires a private admitted actor")
    cash, native = (
        _parse_usd(str(controls.get("cash_budget_usd"))),
        # `hold_budget_usd`: the same figure for a provider with no native cap.
        _parse_usd(str(controls.get("native_budget_usd", controls.get("hold_budget_usd")))),
    )
    with localcontext() as arithmetic:
        arithmetic.prec = 80
        if not markup.is_finite() or markup <= 0 or native <= 0 or cash < native * markup:
            raise FleetConflict("root cash must cover its native budget and markup")
    with session.no_autoflush:
        graph, _ = await fleet_store.lock_budget(
            session,
            tenant_id=uuid.UUID(scope.tenant_id),
            actor_id=uuid.UUID(scope.account_id),
            run_id=None,
            persisted=True,
        )
        record = await mux_state.get_fleet_root_operation(
            session, scope, origin_id=origin_id, for_update=True
        )
        if record is not None:
            finance = record.result.get("root_finance")
            if (
                record.slot != slot
                or record.result.get("root_controls") != dict(controls)
                or not isinstance(finance, dict)
            ):
                raise FleetConflict("original root reservation is unavailable")
        elif intent_id is None:
            raise FleetConflict("original root reservation is unavailable")
        roots = await mux_state.list_fleet_root_finances(
            session, tenant_id=uuid.UUID(scope.tenant_id)
        )
        if record is not None and (_root_money(record)[0] < cash or record not in roots):
            raise FleetConflict("original root reservation is unavailable")
        available, channel_available = await _financial_room(
            session,
            tenant_id=uuid.UUID(scope.tenant_id),
            platform=slot.thread.channel.platform,
            channel_id=slot.thread.channel.channel_id,
            graph=graph,
            roots=roots,
        )
        delta = cash if record is None else Decimal(0)
        if (
            available is None
            or available < delta
            or (channel_available is not None and channel_available < delta)
        ):
            raise FleetConflict("root funding is unavailable")
        if record is None:
            initial_finance: dict[str, JsonValue] = {
                "exposure_usd": str(cash),
                "debit_usd": "0",
                "input_keys": [],
                "input_outcomes": {},
            }
            record = await mux_state.get_fleet_root_operation(
                session,
                scope,
                intent_id=intent_id,
                origin_id=origin_id,
                slot=slot,
                root_controls=controls,
                root_finance=initial_finance,
                request_digest=request_digest(
                    {
                        "session": None,
                        "kind": "root",
                        "request": {
                            "root_controls": dict(controls),
                            "initial_finance": initial_finance,
                        },
                    }
                ),
            )
        assert record is not None
        return record


async def _produce_fleet_budget(
    session: AsyncSession,
    *,
    parent: WorkerPermissions,
    worker_id: uuid.UUID | None,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
    reserve: bool,
    financial: bool,
    snapshot: bool = False,
    locked: tuple[
        fleet_store.BudgetGraph,
        Mapping[uuid.UUID, datetime | None],
        tuple[OperationRecord, ...],
    ]
    | None = None,
) -> dict[uuid.UUID, FleetBudgetUsage]:
    """The sole product producer of held exposure and admission decisions.

    A `snapshot` pass reads the caller's read-only transaction without the tenant
    lock or row locks and persists nothing: it decides from what it would persist.

    Use the store's tenant/actor/run/worker lock order. Read originals, derive
    conservative money once, and persist before returning an admission refusal.
    Terminal/crashed unknowns retain their bound. Only proof of cancellation
    before any claim or native evidence releases the unused reservation.
    Caller owns commit, including refusals that retain new overrun evidence.

    `locked` is the (graph, allocation stamps, roots) the caller has just read with
    `lock_budget(financial=True)` and `list_fleet_root_finances` in this transaction,
    writing nothing since. The locks it holds fence every writer of those rows, so
    reading them again here would return the same rows.
    """
    if not markup.is_finite() or markup <= 0:
        raise FleetConflict("fleet billing markup must be positive and finite")
    if snapshot and reserve:
        raise FleetConflict("a snapshot read cannot reserve")
    if locked is not None and (snapshot or not financial):
        raise FleetConflict("only the financial producer reuses a locked graph")
    facts = parent.fleet
    if facts.scope.is_platform or facts.scope.is_legacy_host_authorized:
        raise FleetConflict("fleet budget requires tenant authority")
    tenant_id = uuid.UUID(facts.scope.tenant_id)
    with session.no_autoflush:
        if locked is None:
            graph, started_at = await fleet_store.lock_budget(
                session,
                tenant_id=tenant_id,
                actor_id=facts.account_id,
                run_id=facts.run_id,
                financial=financial,
                persisted=True,
                locking=not snapshot,
            )
            roots = await mux_state.list_fleet_root_finances(
                session, tenant_id=tenant_id, locking=not snapshot
            )
        else:
            graph, started_at, roots = locked
        # Refuse before any write; the money itself is read after save_budget below.
        _check_financial_room(
            session,
            tenant_id=tenant_id,
            platform=facts.root.channel.platform,
            channel_id=facts.root.channel.channel_id,
            roots=roots,
        )
        pair = next((pair for pair in graph if pair[0].id == facts.run_id), None)
        if pair is None:
            raise FleetConflict("fleet run is unavailable")
        run, workers = pair
        if (worker_id is not None and not any(w.id == worker_id for w in workers)) or (
            run.orchestrator_account_id != facts.account_id
            or facts.scope.account_id != str(run.orchestrator_account_id)
            or (run.platform, run.channel_id, run.thread_id)
            != (facts.root.channel.platform, facts.root.channel.channel_id, facts.root.thread_id)
        ):
            raise FleetConflict("fleet budget belongs to another root")
        actor = await accounts.get_account(session, facts.account_id)
        if actor is None or actor.tenant_id != tenant_id:
            raise FleetConflict("financial admission requires a live tenant actor")
        if facts.root.channel.tenant_id != str(tenant_id):
            raise FleetConflict("root channel differs from tenant authority")
        root_binding = await mux_state.get_binding(
            session, Slot(thread=facts.root, account_id=str(facts.account_id))
        )
        if root_binding is None:
            root_binding = await mux_state.get_binding(
                session, Slot(thread=facts.root, account_id=None)
            )
        if root_binding is None or (
            root_binding.id != run.orchestrator_binding_id
            or root_binding.native_refs.get("session") != run.orchestrator_session_id
        ):
            raise FleetConflict("financial admission requires the current root binding")
        if run.total_budget_usd > parent.limits.budget_usd:
            raise FleetConflict("fleet cap exceeds the authenticated parent budget")
        parent.limits.require_profile(get_profile(root_binding.profile))
        provider_limits = (
            None
            if parent.limits.provider_limits_usd is None
            else {
                str(provider): amount
                for provider, amount in parent.limits.provider_limits_usd.items()
            }
        )
        if provider_limits is not None:
            with localcontext() as arithmetic:
                arithmetic.prec = 80
                if sum(provider_limits.values(), Decimal(0)) > run.total_budget_usd:
                    raise FleetConflict("provider limits exceed the retained run budget")

        # The shared tariff gate covers each selected worker before reservation,
        # dispatch or passive refresh, even without a native binding yet.
        pricing_unavailable: dict[uuid.UUID, bool] = {}
        for member in workers:
            price = prices.get((member.backend, member.model))
            unavailable = price is None or (
                isinstance(price, ProviderPrice)
                and (
                    not price.current_on(datetime.now(UTC).date())
                    or (price.provider, price.model) != (member.backend, member.model)
                )
            )
            if isinstance(price, ModelRates):
                for variant in (price, *((price.long_context,) if price.long_context else ())):
                    for value in (
                        variant.input,
                        variant.output,
                        variant.cache_read,
                        variant.cache_write,
                    ):
                        rate = Decimal(str(value))
                        unavailable = unavailable or not rate.is_finite() or rate < 0
            pricing_unavailable[member.id] = unavailable
        floors: dict[uuid.UUID, Decimal] = {}
        exposures: dict[uuid.UUID, Decimal] = {}
        debits: dict[uuid.UUID, Decimal] = {}
        zero_spend: set[uuid.UUID] = set()
        unpriced_usage = False
        prefetch = await _prefetch_fleet_money(session, graph, tenant_id=tenant_id)
        with localcontext() as arithmetic:
            arithmetic.prec = 80
            for other_run, members in graph:
                debit_total = Decimal(0)
                for member in members:
                    floor, debit, unpriced, no_native_evidence = await _fleet_worker_money(
                        session,
                        other_run,
                        member,
                        scope=facts.scope,
                        prices=prices,
                        markup=markup,
                        price_floor=other_run.id == run.id,
                        prefetch=prefetch,
                    )
                    unpriced_usage = unpriced_usage or unpriced
                    floors[member.id] = floor
                    # An unbound starting/claimed worker is ambiguous. The retained
                    # claim stamp and attempt inventory must also prove no allocation.
                    if (
                        member.status == "cancelled"
                        and started_at[member.id] is None
                        and member.binding_id is None
                        and member.attempts == member.turns_used == 0
                        and no_native_evidence
                    ):
                        zero_spend.add(member.id)
                        exposures[member.id] = debit
                    else:
                        exposures[member.id] = max(member.reserved_usd, floor)
                    debit_total += debit
                debits[other_run.id] = debit_total
            graph = (
                fleet_store.project_budget(
                    graph, exposures=exposures, debits=debits, zero_spend=frozenset(zero_spend)
                )
                if snapshot
                else await fleet_store.save_budget(
                    session,
                    tenant_id=tenant_id,
                    graph=graph,
                    exposures=exposures,
                    debits=debits,
                    zero_spend=frozenset(zero_spend),
                )
            )
            run, workers = next(pair for pair in graph if pair[0].id == run.id)
            available, channel_available = await _financial_room(
                session,
                tenant_id=tenant_id,
                platform=run.platform,
                channel_id=run.channel_id,
                graph=graph,
                roots=roots,
            )
            result: dict[uuid.UUID, FleetBudgetUsage] = {}
            selected = (
                workers if worker_id is None else tuple(w for w in workers if w.id == worker_id)
            )
            worker_overrun = any(w.reserved_usd > w.budget_usd for w in workers)
            provider_exposures: dict[str, Decimal] = {}
            for member in workers:
                provider_exposures[member.backend] = (
                    provider_exposures.get(member.backend, Decimal(0)) + member.reserved_usd
                )
            for worker in selected:
                # Input-bearing creation has a paid claim before a native binding
                # exists. Keep its full reservation while that claim is in flight;
                # absent/unknown receipts never authorize another allocation.
                creating_input = False
                if (
                    worker.status == "starting"
                    and worker.binding_id is None
                    and worker.turns_used == 1
                ):
                    claim = await mux_state.get_operation(
                        session,
                        facts.scope,
                        spawn_operation_key(run.id, worker.ordinal) + ":send:0",
                    )
                    creating_input = (
                        claim is not None
                        and claim.slot == worker_slot(run, worker)
                        and claim.operation.status in ("sent", "processed")
                    )
                reason = None
                if run.status != "running" or worker.status not in (
                    "pending",
                    "starting",
                    "running",
                ):
                    reason = "terminal"
                elif worker.error_code == "stop_requested":
                    reason = "worker_stop_requested"
                elif available is None:
                    reason = "funding_unavailable"
                elif available < 0 or (
                    reserve and run.total_budget_usd > available + run.reserved_usd
                ):
                    reason = "tenant_budget_exceeded"
                elif channel_available is not None and (
                    channel_available < 0
                    or (reserve and run.total_budget_usd > channel_available + run.reserved_usd)
                ):
                    reason = "channel_budget_exceeded"
                elif run.reserved_usd > run.total_budget_usd:
                    reason = "run_budget_exceeded"
                elif provider_limits is not None and any(
                    amount > provider_limits.get(provider, Decimal(0))
                    for provider, amount in provider_exposures.items()
                ):
                    reason = "provider_budget_exceeded"
                elif worker_overrun or floors[worker.id] >= worker.budget_usd:
                    reason = "worker_budget_exceeded"
                elif pricing_unavailable[worker.id] or unpriced_usage:
                    reason = "pricing_unavailable"
                # The accepted initial input is counted by the worker store, not
                # by guard recapture. Its funded running binding stays monitored.
                elif worker.attempts or (
                    worker.turns_used
                    and (
                        worker.turns_used != 1
                        or worker.turns_used > worker.max_turns
                        or (
                            not creating_input
                            and (worker.status != "running" or worker.binding_id is None)
                        )
                    )
                ):
                    reason = "accepted_attempt_inventory_unavailable"
                if reserve and reason is None and worker.reserved_usd < worker.budget_usd:
                    delta = worker.budget_usd - worker.reserved_usd
                    if worker.budget_usd <= 0:
                        reason = "worker_budget_exceeded"
                    elif run.reserved_usd + delta > run.total_budget_usd:
                        reason = "run_budget_exceeded"
                    elif provider_limits is not None and (
                        provider_exposures.get(worker.backend, Decimal(0)) + delta
                        > provider_limits.get(worker.backend, Decimal(0))
                    ):
                        reason = "provider_budget_exceeded"
                    elif available is None or delta > available:
                        reason = "tenant_budget_exceeded"
                    elif channel_available is not None and delta > channel_available:
                        reason = "channel_budget_exceeded"
                    else:
                        exposures[worker.id] = worker.budget_usd
                        graph = await fleet_store.save_budget(
                            session,
                            tenant_id=tenant_id,
                            graph=graph,
                            exposures=exposures,
                            debits=debits,
                            zero_spend=frozenset(zero_spend),
                        )
                        run, workers = next(pair for pair in graph if pair[0].id == run.id)
                        worker = next(w for w in workers if w.id == worker_id)
                        available -= delta
                        if channel_available is not None:
                            channel_available -= delta
                admitted = reason is None and worker.reserved_usd >= worker.budget_usd > 0
                result[worker.id] = FleetBudgetUsage(
                    run, worker, floors[worker.id], available, channel_available, admitted, reason
                )
        return result


async def produce_fleet_budget(
    session: AsyncSession,
    *,
    parent: WorkerPermissions,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
) -> dict[uuid.UUID, FleetBudgetUsage]:
    """Refresh one run's worker decisions from one durable graph pass.

    Passive facts never authorize native dispatch. Concurrent ordinary ledger
    writes may change available funds; fresh admission repeats this producer
    with the financial fence before allocating anything.
    """
    return await _produce_fleet_budget(
        session,
        parent=parent,
        worker_id=None,
        prices=prices,
        markup=markup,
        reserve=False,
        financial=False,
    )


async def read_fleet_budget_snapshot(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    parent: WorkerPermissions,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
) -> dict[uuid.UUID, FleetBudgetUsage]:
    """The passive producer's decisions from one consistent read-only snapshot.

    Takes no tenant lock and no row lock, so admissions holding them never delay
    it; writes nothing, so it never replaces the locked refresh that persists floors.
    """
    async with sessionmaker.begin() as session:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        return await _produce_fleet_budget(
            session,
            parent=parent,
            worker_id=None,
            prices=prices,
            markup=markup,
            reserve=False,
            financial=False,
            snapshot=True,
        )


async def _reserve_fleet_worker(
    session: AsyncSession,
    *,
    parent: WorkerPermissions,
    policy: TenantAccessPolicy,
    worker_id: uuid.UUID,
    expected_version: int,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
    roots: tuple[OperationRecord, ...],
) -> FleetBudgetUsage:
    """Financial admission in the SAME caller transaction as canonical spawn.

    Call before any provider allocation; pool.claim alone is not admission.
    Consumer commits the returned refusal too, preserving overrun evidence, and
    dispatches only an admitted pending/starting first attempt. Unknown native
    attempts/replacement and operator-funded tenants remain unavailable here.
    """
    decision = authorize(
        policy,
        subject=parent.subject,
        action=Action.SPAWN_WORKER,
        surface=Surface.FLEET,
        agent=parent.agent,
        place=parent.fleet.place,
        fleet=parent.fleet,
    )
    if not decision:
        raise FleetConflict("reservation requires current root authorization")
    with session.no_autoflush:
        # Read again after the caller's add_worker, which this graph must include. The
        # roots are the caller's: they were read and locked under this same lock, and
        # add_worker does not write them.
        graph, started_at = await fleet_store.lock_budget(
            session,
            tenant_id=uuid.UUID(parent.fleet.scope.tenant_id),
            actor_id=parent.fleet.account_id,
            run_id=parent.fleet.run_id,
            persisted=True,
        )
        pair = next((p for p in graph if p[0].id == parent.fleet.run_id), None)
        if pair is None:
            raise FleetConflict("fleet run is unavailable")
        worker = next((w for w in pair[1] if w.id == worker_id), None)
        if worker is None:
            raise FleetConflict("worker is unavailable")
        if worker.reserved_usd == 0 and worker.version != expected_version:
            raise FleetConflict("worker changed before reservation")
        if worker.status not in ("pending", "starting") or worker.binding_id is not None:
            raise FleetConflict("a new reservation requires an unallocated worker")
        # Spec attenuation is the resolver/driver's responsibility. Financial
        # admission repeats the shared root authorization, not its spec compiler.
        if worker.budget_usd > parent.limits.budget_usd:
            raise FleetConflict("worker reservation exceeds parent authority")
        snapshot = (
            await _produce_fleet_budget(
                session,
                parent=parent,
                worker_id=worker_id,
                prices=prices,
                markup=markup,
                reserve=True,
                financial=True,
                locked=(graph, started_at, roots),
            )
        )[worker_id]
        if snapshot.admitted and worker.reserved_usd == 0:
            audit = await audit_worker_action(
                session,
                parent,
                worker_id=worker_id,
                operation="spawn",
                outcome="allowed",
                reason="authorized",
            )
            if audit is None:
                raise FleetConflict("reservation requires a retained actor audit")
        return snapshot


#: Consecutive slow budget reads tolerated before a run's workers are cancelled.
BUDGET_READ_TIMEOUT_LIMIT = 3
#: Seconds one transaction may hold the tenant fleet lock against the watch before it
#: fails closed. A holder this long is stuck (or holding over slow work), not admitting.
BUDGET_LOCK_BUSY_LIMIT_S = 10.0
#: Seconds the lock may stay busy while its holders keep changing (admissions making
#: progress) before the watch fails closed anyway. Busy polls still decide from a
#: lock-free snapshot, so this bounds how long the locked refresh cannot persist floors,
#: not how long usage goes unread.
BUDGET_LOCK_CONTENDED_LIMIT_S = 60.0
#: Seconds between a run watch's locked refreshes (which persist floors). Polls between
#: them decide from the lock-free snapshot, so the watch no longer holds the tenant fleet
#: lock four times a second per run while every admission in the tenant waits on it.
BUDGET_LOCKED_REFRESH_S = 2.0

# The granted holder of one advisory key, from the same transaction the watch opened.
_LOCK_HOLDER = text(
    """
    SELECT a.pid, a.xact_start, a.state, a.wait_event_type, a.wait_event,
           extract(epoch FROM clock_timestamp() - a.xact_start) AS xact_s,
           a.query_id
    FROM pg_locks AS l JOIN pg_stat_activity AS a ON a.pid = l.pid
    WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1
      AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND l.classid = ((hashtextextended(:key, 0) >> 32) & 4294967295)::oid
      AND l.objid = (hashtextextended(:key, 0) & 4294967295)::oid
    ORDER BY a.xact_start, a.pid
    LIMIT 1
    """
)


class _BudgetSnapshotDue(Exception):
    """This poll decides from the lock-free snapshot; the locked refresh ran recently."""


class _BudgetLockBusy(Exception):
    """The tenant fleet lock is held by a writer; this poll is skipped, not failed."""

    def __init__(self, holder: dict[str, object] | None) -> None:
        self.holder = holder
        super().__init__("tenant fleet lock busy")


async def _lock_holder(session: AsyncSession, key: str) -> dict[str, object] | None:
    """Who holds the key now, for the log and the stuck check; None if not visible."""
    try:
        async with session.begin_nested():
            row = (await session.execute(_LOCK_HOLDER, {"key": key})).mappings().first()
    except DBAPIError:
        return None
    if row is None:
        return None
    return {
        "pid": row["pid"],
        "xact_start": row["xact_start"].isoformat() if row["xact_start"] else None,
        "state": row["state"],
        "wait": f"{row['wait_event_type']}:{row['wait_event']}",
        "xact_s": round(float(row["xact_s"]), 3) if row["xact_s"] is not None else None,
        "query_id": row["query_id"],
    }


type WorkerCancelReason = Literal[
    "terminal",
    "budget",
    "usage_unavailable",
    "watch_cancelled",
    "watch_ended",
    "run_deadline",
    "shutdown",
]


class WorkerCancel(asyncio.Event):
    """A worker's cancel event that remembers why it was first set (`cancel_workers`)."""

    def __init__(self) -> None:
        super().__init__()
        self.reason: WorkerCancelReason | None = None


def cancel_workers(
    events: Mapping[uuid.UUID, asyncio.Event],
    reason: WorkerCancelReason,
    *,
    run_id: uuid.UUID | None,
    detail: str | None = None,
) -> None:
    """Set workers' cancel events, logging why once, with a closed category.

    A worker interrupted at send or stream open shows only `interrupted`; this line
    names the cause. Events already set are left alone and not logged again.
    """
    pending = [(worker_id, event) for worker_id, event in events.items() if not event.is_set()]
    if not pending:
        return
    (log.info if reason == "terminal" else log.warning)(
        "fleet.worker_cancel",
        reason=reason,
        detail=detail,
        run_id=str(run_id) if run_id is not None else None,
        worker_ids=[str(worker_id) for worker_id, _ in pending],
    )
    for _, event in pending:
        if isinstance(event, WorkerCancel):
            event.reason = reason
        event.set()


async def watch_fleet_budget(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    parent: WorkerPermissions,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
    cancelled: Mapping[uuid.UUID, asyncio.Event],
    finished: asyncio.Event,
    poll_interval: float = 0.25,
    read_timeout: float | None = None,
    locked_refresh_s: float = BUDGET_LOCKED_REFRESH_S,
) -> str:
    """One loop per run, fanning out decisions from the same producer graph.

    Poll at four Hz by default, counting producer time inside each interval.
    run_prepared_turn owns the only worker deadline. Unknown reservations at
    the run bound remain held; verified worker floors at their bound cancel.
    Caller registers each worker's cancellation event in the same mapping only
    after its admission commits, and sets finished when the run ends. A cancelled or
    completed cohort never ends the active run watch; unavailable usage still
    fails closed and requests cancellation of all registered workers. While the tenant
    lock is busy, each poll decides from a lock-free read-only snapshot of the same
    producer, so contention never stops overrun cancellation. The holder is logged;
    the watch fails closed when one transaction holds the lock past
    BUDGET_LOCK_BUSY_LIMIT_S or it stays busy past BUDGET_LOCK_CONTENDED_LIMIT_S.
    """
    if parent.fleet.enabled is not True or parent.fleet.depth != 0:
        raise FleetConflict("fleet watch requires explicit root opt-in")
    timeout = read_timeout if read_timeout is not None else poll_interval * 8
    if not all(math.isfinite(v) and v > 0 for v in (poll_interval, timeout)):
        raise ValueError("budget poll and read timeout must be positive finite durations")
    timeouts = 0
    busy_since: float | None = None
    holder_key: tuple[object, object] | None = None
    holder_since: float | None = None
    lock_key = f"fleet:{uuid.UUID(parent.fleet.scope.tenant_id)}"
    # Wait in line for the tenant lock, but leave room inside the read timeout.
    lock_wait_ms = max(1, int(min(1.5, timeout * 0.75) * 1000))
    last_locked: float | None = None
    try:
        while not finished.is_set():
            started = asyncio.get_running_loop().time()
            watched = tuple(cancelled.items())
            snapshot_read = False
            try:
                if last_locked is not None and started - last_locked < locked_refresh_s:
                    raise _BudgetSnapshotDue
                # The read gets its whole timeout after the lock wait, so a lock granted
                # late in the wait is not then counted as a slow read.
                async with asyncio.timeout(timeout + lock_wait_ms / 1000):
                    async with sessionmaker.begin() as session:
                        # Admissions take the tenant fleet lock repeatedly during dispatch; a held
                        # lock past the short wait below is busy, not unavailable usage.
                        # Queue fairly behind writers (try-lock starved behind the
                        # admissions' repeated short holds), but only briefly.
                        await session.execute(text(f"SET LOCAL lock_timeout = '{lock_wait_ms}ms'"))
                        try:
                            # A savepoint keeps the transaction usable to name the holder.
                            async with session.begin_nested():
                                await session.execute(
                                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                                    {"key": lock_key},
                                )
                        except DBAPIError as error:
                            if (
                                "lock timeout" not in str(error.orig).lower()
                                and getattr(error.orig, "sqlstate", None) != "55P03"
                            ):
                                raise
                            raise _BudgetLockBusy(await _lock_holder(session, lock_key)) from None
                        await session.execute(text("SET LOCAL lock_timeout = 0"))
                        usages = await produce_fleet_budget(
                            session,
                            parent=parent,
                            prices=prices,
                            markup=markup,
                        )
                        if any(worker_id not in usages for worker_id, _ in watched):
                            raise FleetConflict("watched worker is unavailable")
            except (_BudgetLockBusy, _BudgetSnapshotDue) as skipped:
                if isinstance(skipped, _BudgetLockBusy):
                    now = asyncio.get_running_loop().time()
                    busy_since = busy_since if busy_since is not None else now
                    holder = skipped.holder
                    # One holder transaction is its backend and start; an unseen holder
                    # counts as one that never changes, as before.
                    current = (
                        (holder["pid"], holder["xact_start"])
                        if holder is not None
                        else (None, None)
                    )
                    if current != holder_key or holder_since is None:
                        holder_key, holder_since = current, now
                        log.info("fleet.budget_lock_busy", holder=holder)
                    stuck = now - holder_since >= BUDGET_LOCK_BUSY_LIMIT_S
                    contended = now - busy_since >= BUDGET_LOCK_CONTENDED_LIMIT_S
                    if stuck or contended:
                        log.warning(
                            "fleet.budget_read_unavailable",
                            reason="lock_busy",
                            error_class="BudgetLockBusy",
                            holder=holder,
                            busy_s=round(now - busy_since, 3),
                            holder_s=round(now - holder_since, 3),
                        )
                        cancel_workers(cancelled, "usage_unavailable", run_id=parent.fleet.run_id)
                        return "usage_unavailable"
                # Contention must not blind the watch: decide from a lock-free,
                # read-only snapshot of the same producer. The locked refresh that
                # persists floors resumes when the lock is free.
                try:
                    async with asyncio.timeout(timeout):
                        usages = await read_fleet_budget_snapshot(
                            sessionmaker, parent=parent, prices=prices, markup=markup
                        )
                    if any(worker_id not in usages for worker_id, _ in watched):
                        raise FleetConflict("watched worker is unavailable")
                except TimeoutError:
                    timeouts += 1
                    if timeouts < BUDGET_READ_TIMEOUT_LIMIT:
                        log.info("fleet.budget_read_slow", consecutive=timeouts, snapshot=True)
                        await asyncio.sleep(poll_interval)
                        continue
                    log.warning(
                        "fleet.budget_read_unavailable",
                        reason="read_unavailable",
                        error_class="TimeoutError",
                        snapshot=True,
                    )
                    cancel_workers(cancelled, "usage_unavailable", run_id=parent.fleet.run_id)
                    return "usage_unavailable"
                except Exception as error:
                    log.warning(
                        "fleet.budget_read_unavailable",
                        reason="read_unavailable",
                        error_class=type(error).__name__,
                        snapshot=True,
                    )
                    cancel_workers(cancelled, "usage_unavailable", run_id=parent.fleet.run_id)
                    return "usage_unavailable"
                snapshot_read = True
            except TimeoutError:
                # One slow read (e.g. while sibling admissions hold the run lock)
                # is not unavailable usage: retry, and fail closed only after
                # consecutive timeouts. Holds are unchanged meanwhile.
                timeouts += 1
                if timeouts < BUDGET_READ_TIMEOUT_LIMIT:
                    log.info("fleet.budget_read_slow", consecutive=timeouts)
                    continue
                log.warning(
                    "fleet.budget_read_unavailable",
                    reason="read_unavailable",
                    error_class="TimeoutError",
                )
                cancel_workers(cancelled, "usage_unavailable", run_id=parent.fleet.run_id)
                return "usage_unavailable"
            except Exception as error:
                log.warning(
                    "fleet.budget_read_unavailable",
                    reason="read_unavailable",
                    error_class=type(error).__name__,
                )
                cancel_workers(cancelled, "usage_unavailable", run_id=parent.fleet.run_id)
                return "usage_unavailable"
            timeouts = 0
            if not snapshot_read:
                busy_since, holder_key, holder_since = None, None, None
                last_locked = started
            for worker_id, event in watched:
                usage = usages.get(worker_id)
                if event.is_set() or usage is None:
                    continue
                if usage.reason == "terminal":
                    cancel_workers({worker_id: event}, "terminal", run_id=parent.fleet.run_id)
                elif not usage.admitted:
                    log.warning("fleet.budget_cancel", reason=usage.reason or "not_admitted")
                    cancel_workers(
                        {worker_id: event},
                        "budget",
                        run_id=parent.fleet.run_id,
                        detail=usage.reason or "not_admitted",
                    )
            delay = max(0, poll_interval - (asyncio.get_running_loop().time() - started))
            with suppress(TimeoutError):
                await asyncio.wait_for(finished.wait(), timeout=delay)
        return "finished"
    except asyncio.CancelledError:
        cancel_workers(cancelled, "watch_cancelled", run_id=parent.fleet.run_id)
        raise


async def require_fleet_worker_admission(
    session: AsyncSession,
    *,
    parent: WorkerPermissions,
    policy: TenantAccessPolicy,
    worker_id: uuid.UUID,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
    binding: ProviderBinding | None = None,
) -> FleetBudgetUsage:
    """Fresh financial readback before allocation or a bound worker's input.

    Never implicitly funds a claimed worker. Return refusal rather than raise
    after producing money, so callers commit overrun evidence, then stop before
    native allocation/input. With no binding, require an unallocated starting
    claim. With the actual prepared binding, require the running row and an
    exact current private-slot identity; stale snapshots convey no permission.
    The resolver/driver still proves fresh canonical spec/model/resources.
    """
    decision = authorize(
        policy,
        subject=parent.subject,
        action=Action.SPAWN_WORKER,
        surface=Surface.FLEET,
        agent=parent.agent,
        place=parent.fleet.place,
        fleet=parent.fleet,
    )
    if not decision:
        raise FleetConflict("native dispatch requires current root authority")
    usage = (
        await _produce_fleet_budget(
            session,
            parent=parent,
            worker_id=worker_id,
            prices=prices,
            markup=markup,
            reserve=False,
            financial=True,
        )
    )[worker_id]
    if binding is None:
        if usage.worker.status != "starting" or usage.worker.binding_id is not None:
            return replace(usage, admitted=False, reason="not_an_unallocated_pool_claim")
    elif (
        usage.worker.status != "running"
        or usage.worker.binding_id != binding.id
        or not binding.native_refs.get("session")
        or binding != await mux_state.get_binding(session, worker_slot(usage.run, usage.worker))
    ):
        return replace(usage, admitted=False, reason="not_current_worker_binding")
    return usage


async def admit_fleet_worker(
    session: AsyncSession,
    *,
    parent: WorkerPermissions,
    policy: TenantAccessPolicy,
    resolved: ResolvedWorkerSpec,
    ordinal: int,
    role: str,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
    reviewer_of: uuid.UUID | None = None,
    result_contract: Mapping[str, JsonValue] | None = None,
) -> FleetBudgetUsage:
    """Canonical spawn plus reservation in the financial lock order.

    The host supplies a freshly resolved spec from the sole owner resolver.
    Lock before add_worker; taking run locks first across two runs could invert
    the tenant money lock. Native creation starts only after this commits.
    """
    decision = authorize(
        policy,
        subject=parent.subject,
        action=Action.SPAWN_WORKER,
        surface=Surface.FLEET,
        agent=parent.agent,
        place=parent.fleet.place,
        fleet=parent.fleet,
    )
    if not decision:
        raise FleetConflict("spawn requires current root authorization")
    if (
        resolved.permissions.fleet.run_id != parent.fleet.run_id
        or resolved.permissions.fleet.origin_id != parent.fleet.origin_id
        or resolved.permissions.fleet.scope.account_id != parent.fleet.scope.account_id
        or resolved.permissions.fleet.scope.tenant_id != parent.fleet.scope.tenant_id
        or resolved.permissions.fleet.depth != 1
    ):
        raise FleetConflict("resolved worker belongs to another root")
    tenant_id = uuid.UUID(parent.fleet.scope.tenant_id)
    with session.no_autoflush:
        await fleet_store.lock_budget(
            session,
            tenant_id=tenant_id,
            actor_id=parent.fleet.account_id,
            run_id=parent.fleet.run_id,
            persisted=True,
        )
        roots = await mux_state.list_fleet_root_finances(session, tenant_id=tenant_id)
        # Refuse before add_worker writes; the reservation reads the money.
        _check_financial_room(
            session,
            tenant_id=tenant_id,
            platform=parent.fleet.root.channel.platform,
            channel_id=parent.fleet.root.channel.channel_id,
            roots=roots,
        )
        worker = await fleet_store.add_worker(
            session,
            tenant_id=tenant_id,
            run_id=parent.fleet.run_id,
            ordinal=ordinal,
            role=role,
            spec=resolved.spec.model_dump(mode="json"),
            spec_digest=resolved.digest,
            backend=resolved.backend.backend,
            profile=resolved.backend.profile,
            model=resolved.spec.model.id,
            budget_usd=resolved.limits.budget_usd,
            deadline_at=resolved.limits.deadline,
            max_turns=resolved.limits.max_turns,
            reviewer_of=reviewer_of,
            result_contract=result_contract,
        )
        usage = await _reserve_fleet_worker(
            session,
            parent=parent,
            policy=policy,
            worker_id=worker.id,
            expected_version=worker.version,
            prices=prices,
            markup=markup,
            roots=roots,
        )
        if not usage.admitted and usage.worker.status == "pending":
            ended = await fleet_store.finish(
                session,
                tenant_id=tenant_id,
                worker_id=worker.id,
                expected_version=usage.worker.version,
                status="cancelled",
                error_code=usage.reason or "not_admitted",
            )
            usage = replace(usage, worker=ended)
        return usage
