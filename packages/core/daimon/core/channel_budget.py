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
from typing import get_args

import structlog
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Decision, Place, Subject, Surface, authorize
from daimon.core.errors import DaimonError
from daimon.core.fleet import FleetConflict, FleetRun, FleetWorker, worker_slot
from daimon.core.neutral_resources import (
    ResolvedWorkerSpec,
    WorkerPermissions,
    audit_worker_action,
)
from daimon.core.pricing import ModelRates, ProviderPrice
from daimon.core.stores import accounts, mux_state, tenant_ledger
from daimon.core.stores import fleet as fleet_store
from daimon.core.stores.channel_budgets import get_channel_budget, list_channel_budgets
from daimon.core.stores.domain import BudgetWindow, ChannelBudgetRow
from daimon.core.stores.tenant_ledger import get_channel_spend, get_prepaid_balance
from daimon.core.stores.turn_usage import read_accounting_revisions
from daimon.core.usage_delegation import HostUsageBinding, read_delegated_usage
from mux.contracts.ids import Scope
from mux.contracts.resources import ProviderBinding
from mux.profiles import get_profile
from mux.state.lease import Slot
from pydantic import JsonValue
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
) -> tuple[Decimal, Decimal, bool, bool]:
    binding = await mux_state.get_binding(session, worker_slot(run, worker))
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
    rows = await read_accounting_revisions(session, tenant_id=run.tenant_id, binding=binding)
    with localcontext() as arithmetic:
        arithmetic.prec = 80
        debit = -sum((row.delta_usd for row in rows.ledger), Decimal(0))
        if debit < 0:
            raise FleetConflict("worker ledger cannot prove nonnegative spend")
        if not rows.revisions or not price_floor:
            return debit, debit, False, False
        native = rows.revisions[0].observation.session
        usage = await read_delegated_usage(
            session, scope=scope, bindings=(HostUsageBinding(binding, native),), prices=prices
        )
        floor = (usage.token_floor_usd * markup).quantize(
            Decimal("0.000001"), rounding=ROUND_CEILING
        )
        return max(floor, debit), debit, bool(usage.unpriced_observations), False


async def _produce_fleet_budget(
    session: AsyncSession,
    *,
    parent: WorkerPermissions,
    worker_id: uuid.UUID | None,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
    reserve: bool,
    financial: bool,
) -> dict[uuid.UUID, FleetBudgetUsage]:
    """The sole product producer of held exposure and admission decisions.

    Use the store's tenant/actor/run/worker lock order. Read originals, derive
    conservative money once, and persist before returning an admission refusal.
    Terminal/crashed unknowns retain their bound. Only proof of cancellation
    before any claim or native evidence releases the unused reservation.
    Caller owns commit, including refusals that retain new overrun evidence.
    """
    if not markup.is_finite() or markup <= 0:
        raise FleetConflict("fleet billing markup must be positive and finite")
    facts = parent.fleet
    if facts.scope.is_platform or facts.scope.is_legacy_host_authorized:
        raise FleetConflict("fleet budget requires tenant authority")
    tenant_id = uuid.UUID(facts.scope.tenant_id)
    graph, started_at = await fleet_store.lock_budget(
        session,
        tenant_id=tenant_id,
        actor_id=facts.account_id,
        run_id=facts.run_id,
        financial=financial,
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
            str(provider): amount for provider, amount in parent.limits.provider_limits_usd.items()
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
                not price.verified
                or (price.provider, price.model) != (member.backend, member.model)
                or price.checked_on > datetime.now(UTC).date()
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
        graph = await fleet_store.save_budget(
            session,
            tenant_id=tenant_id,
            graph=graph,
            exposures=exposures,
            debits=debits,
            zero_spend=frozenset(zero_spend),
        )
        run, workers = next(pair for pair in graph if pair[0].id == run.id)
        residual = sum((r.reserved_usd - r.observed_usd for r, _ in graph), Decimal(0))
        # Read original debits before balance: concurrent new debits can only
        # conservatively reduce passive room. Financial admission fences writers.
        balance = await tenant_ledger.get_prepaid_balance(session, tenant_id=tenant_id)
        available = None if balance is None else balance - residual
        channel = await get_channel_budget_status(
            session,
            tenant_id=tenant_id,
            platform=run.platform,
            channel_id=run.channel_id,
            now=datetime.now(UTC),
        )
        channel_available: Decimal | None = None
        if channel is not None and channel.is_active:
            held = sum(
                (
                    r.reserved_usd - r.observed_usd
                    for r, _ in graph
                    if (r.platform, r.channel_id) == (run.platform, run.channel_id)
                ),
                Decimal(0),
            )
            channel_available = channel.remaining_usd - held
        result: dict[uuid.UUID, FleetBudgetUsage] = {}
        selected = workers if worker_id is None else tuple(w for w in workers if w.id == worker_id)
        worker_overrun = any(w.reserved_usd > w.budget_usd for w in workers)
        provider_exposures: dict[str, Decimal] = {}
        for member in workers:
            provider_exposures[member.backend] = (
                provider_exposures.get(member.backend, Decimal(0)) + member.reserved_usd
            )
        for worker in selected:
            reason = None
            if run.status != "running" or worker.status not in ("pending", "starting", "running"):
                reason = "terminal"
            elif worker.error_code == "stop_requested":
                reason = "worker_stop_requested"
            elif balance is None:
                reason = "funding_unavailable"
            elif available is not None and (
                available < 0 or (reserve and run.total_budget_usd > available + run.reserved_usd)
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
                    or worker.status != "running"
                    or worker.binding_id is None
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


async def _reserve_fleet_worker(
    session: AsyncSession,
    *,
    parent: WorkerPermissions,
    policy: TenantAccessPolicy,
    worker_id: uuid.UUID,
    expected_version: int,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
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
    graph, _ = await fleet_store.lock_budget(
        session,
        tenant_id=uuid.UUID(parent.fleet.scope.tenant_id),
        actor_id=parent.fleet.account_id,
        run_id=parent.fleet.run_id,
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
) -> str:
    """One loop per run, fanning out decisions from the same producer graph.

    Poll at four Hz by default, counting producer time inside each interval.
    run_prepared_turn owns the only worker deadline. Unknown reservations at
    the run bound remain held; verified worker floors at their bound cancel.
    Caller registers each worker's cancellation event in the same mapping only
    after its admission commits, and sets finished when the run ends. A cancelled or
    completed cohort never ends the active run watch; unavailable usage still
    fails closed and requests cancellation of all registered workers.
    """
    if parent.fleet.enabled is not True or parent.fleet.depth != 0:
        raise FleetConflict("fleet watch requires explicit root opt-in")
    timeout = read_timeout if read_timeout is not None else poll_interval * 8
    if not all(math.isfinite(v) and v > 0 for v in (poll_interval, timeout)):
        raise ValueError("budget poll and read timeout must be positive finite durations")
    try:
        while not finished.is_set():
            started = asyncio.get_running_loop().time()
            watched = tuple(cancelled.items())
            try:
                async with asyncio.timeout(timeout):
                    async with sessionmaker.begin() as session:
                        usages = await produce_fleet_budget(
                            session,
                            parent=parent,
                            prices=prices,
                            markup=markup,
                        )
                        if any(worker_id not in usages for worker_id, _ in watched):
                            raise FleetConflict("watched worker is unavailable")
            except Exception as error:
                log.warning(
                    "fleet.budget_read_unavailable",
                    reason="read_unavailable",
                    error_class=type(error).__name__,
                )
                for event in cancelled.values():
                    event.set()
                return "usage_unavailable"
            for worker_id, event in watched:
                usage = usages.get(worker_id)
                if event.is_set() or usage is None:
                    continue
                if usage.reason == "terminal":
                    event.set()
                elif not usage.admitted:
                    log.warning("fleet.budget_cancel", reason=usage.reason or "not_admitted")
                    event.set()
            delay = max(0, poll_interval - (asyncio.get_running_loop().time() - started))
            with suppress(TimeoutError):
                await asyncio.wait_for(finished.wait(), timeout=delay)
        return "finished"
    except asyncio.CancelledError:
        for event in cancelled.values():
            event.set()
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
    await fleet_store.lock_budget(
        session, tenant_id=tenant_id, actor_id=parent.fleet.account_id, run_id=parent.fleet.run_id
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
