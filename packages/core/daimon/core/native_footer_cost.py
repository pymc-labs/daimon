"""Opted-in, root-bound native cost display; accounting remains authoritative.

The host supplies resolved rates after recording real usage. A terminal native
walk and a coherent read of settled revisions are required before displaying a
number. Renderers only read this attempt's result; they never discover tariffs.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from typing import Literal

import structlog
from daimon.core.pricing import ProviderPrice, provider_cost_of
from daimon.core.stores.turn_usage import UsageAccountingRows, read_accounting_revisions
from daimon.core.usage_aggregation import disjoint_observations, replace_observation
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.profiles import accounting_mode_for
from mux.state.usage_ledger import AppliedUsage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()


@dataclass(frozen=True)
class NativeFooterCost:
    status: Literal["complete", "unknown", "partial"]
    reason: str
    amount: Decimal | None = None

    @property
    def display(self) -> str:
        if self.status == "complete" and self.amount is not None:
            if self.amount < Decimal("0.001"):
                return "<$0.001"
            return "$" + f"{self.amount:.3f}".rstrip("0").rstrip(".")
        return "Cost partial" if self.status == "partial" else "Cost unknown"


@dataclass(frozen=True)
class _PricedRevision:
    usage: UsageObservation
    price: ProviderPrice | None
    infrastructure_usd: Decimal | None
    infrastructure_seconds: Decimal | None
    settled: bool


_CURRENT: ContextVar[NativeFooterCostContext | None] = ContextVar(
    "native_footer_cost_attempt", default=None
)


@dataclass
class NativeFooterCostContext:
    """Created only for an explicitly opted-in, admitted native attempt."""

    sessions: async_sessionmaker[AsyncSession]
    scope: Scope
    binding: ProviderBinding
    session: ResourceRef
    markup: Decimal
    root_turn_id: str | None = None
    result: NativeFooterCost = field(
        default_factory=lambda: NativeFooterCost("unknown", "NO_USAGE")
    )
    finished: bool = False
    _samples: dict[tuple[str, int], _PricedRevision] = field(
        default_factory=lambda: dict[tuple[str, int], _PricedRevision]()
    )
    _walk: tuple[UsageObservation, ...] | None = None
    _invalid: str | None = None

    def __post_init__(self) -> None:
        uuid.UUID(self.scope.tenant_id)
        if (
            not accounting_mode_for(self.session.provider).settles_natively
            or self.scope.platform_reason is not None
            or self.scope.legacy_call_site is not None
            or self.session.kind != "session"
            or self.session.tenant_id != self.scope.tenant_id
            or self.session.account_id != self.scope.account_id
            or self.binding.provider != self.session.provider
            or self.binding.thread.channel.tenant_id != self.scope.tenant_id
            or self.binding.native_refs.get("session") != self.session.id
            or not self.markup.is_finite()
            or self.markup < 0
        ):
            raise ValueError("native footer requires an authorized native tenant binding")

    @contextmanager
    def activate(self) -> Iterator[None]:
        token = _CURRENT.set(self)
        try:
            yield
        finally:
            _CURRENT.reset(token)

    def note(
        self,
        usage: UsageObservation,
        *,
        price: ProviderPrice | None,
        infrastructure_usd: Decimal | None,
        settled: bool,
        infrastructure_seconds: Decimal | None = None,
    ) -> None:
        if usage.session != self.session or not usage.turn_id:
            self._invalid = "USAGE_SCOPE_UNKNOWN"
            return
        if self.root_turn_id is None:
            self.root_turn_id = usage.turn_id
        if usage.turn_id != self.root_turn_id:
            self._invalid = "ROOT_MISMATCH"
            return
        self.finished = False
        self.result = NativeFooterCost("partial", "NOT_FINALIZED")
        self._samples[usage.id, usage.revision] = _PricedRevision(
            usage, price, infrastructure_usd, infrastructure_seconds, settled
        )

    def reconciled(self, values: Sequence[UsageObservation]) -> None:
        """Only the existing bounded native replay supplies this exhausted walk."""
        latest: tuple[UsageObservation, ...] = ()
        for value in values:
            latest = replace_observation(latest, value)
        self._walk = latest

    async def finish(self, *, authoritative_terminal: bool) -> None:
        self.finished = True
        self.result = NativeFooterCost("partial" if self._samples else "unknown", "INCOMPLETE")
        if not authoritative_terminal or not self._walk or not self._samples or self._invalid:
            return
        try:
            async with asyncio.timeout(2.0):
                async with self.sessions() as db, db.begin():
                    await db.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    )
                    rows = await read_accounting_revisions(
                        db, tenant_id=uuid.UUID(self.scope.tenant_id), binding=self.binding
                    )
                self.result = self._evaluate(rows)
        except Exception as error:
            # Display is delivery, not accounting. Never expose database strings
            # or turn an unavailable read into a fabricated numeric subtotal.
            log.warning("turn.native_footer_cost_unavailable", error_class=type(error).__name__)
            self.result = NativeFooterCost(
                "partial" if self._samples else "unknown", "READ_UNAVAILABLE"
            )

    def _evaluate(self, rows: UsageAccountingRows) -> NativeFooterCost:
        def pending(reason: str) -> NativeFooterCost:
            return NativeFooterCost("partial" if self._samples else "unknown", reason)

        if rows.binding != self.binding or not self.root_turn_id or not self._walk:
            return pending("BINDING_OR_INVENTORY_UNKNOWN")
        latest: tuple[UsageObservation, ...] = ()
        for value in self._walk:
            if value.session != self.session or value.turn_id != self.root_turn_id:
                return pending("WALK_SCOPE_MISMATCH")
            latest = replace_observation(latest, value)
        if len(latest) > 500 or any(v.covers or v.grain == "session" for v in latest):
            return pending("AGGREGATE_OR_SESSION_COVERAGE_UNSUPPORTED")
        selected = disjoint_observations(latest)
        membership = {v.id: v.revision for v in selected}
        recorded: tuple[UsageObservation, ...] = ()
        for sample in self._samples.values():
            recorded = replace_observation(recorded, sample.usage)
        if {v.id: v.revision for v in recorded} != membership:
            return pending("INVENTORY_INCOMPLETE")
        durable: tuple[UsageObservation, ...] = ()
        root_revisions: list[AppliedUsage] = []
        for item in rows.revisions:
            value = item.observation
            if value.session != self.session or value.turn_id is None:
                return pending("DURABLE_ATTRIBUTION_UNKNOWN")
            if value.turn_id == self.root_turn_id:
                root_revisions.append(item)
                durable = replace_observation(durable, value)
        if {v.id: v.revision for v in durable} != membership:
            return pending("DURABLE_INVENTORY_MISMATCH")
        outbox = {(v.row.observation_id, v.row.revision): v for v in rows.outbox}
        expected = Decimal(0)
        with localcontext() as arithmetic:
            arithmetic.prec = 80
            for value in durable:
                sample = self._samples.get((value.id, value.revision))
                if sample is None or not sample.settled:
                    return pending("SETTLEMENT_PENDING")
                # A raw native meter may omit model; only host-authorized model
                # binding in the recording closure supplies the priced projection.
                walk_value = next(v for v in selected if v.id == value.id)
                projected = walk_value.model_copy(update={"model": sample.usage.model})
                excluded = {"observed_at", "native_revision"}
                if (
                    projected.model_dump(exclude=excluded) != value.model_dump(exclude=excluded)
                    or sample.usage.model_dump(exclude=excluded)
                    != value.model_dump(exclude=excluded)
                    or (walk_value.model is not None and walk_value.model != value.model)
                ):
                    return pending("REVISION_MISMATCH")
                price = sample.price
                if (
                    price is None
                    or not price.verified
                    or price.checked_on > value.observed_at.date()
                    or value.model is None
                    or (price.provider, price.model) != (value.model.provider, value.model.id)
                    or value.completeness != "measured"
                    or not value.native_meter
                ):
                    return pending("USAGE_OR_RATES_UNKNOWN")
                infrastructure = sample.infrastructure_usd
                seconds = sample.infrastructure_seconds
                if (seconds is not None and (not seconds.is_finite() or seconds < 0)) or (
                    infrastructure is not None and infrastructure > 0 and seconds is None
                ):
                    return pending("CONTAINER_TIME_UNKNOWN")
                cost = provider_cost_of(value, price, infrastructure_usd=infrastructure)
                if cost is None:
                    return pending("COST_UNKNOWN")
                expected += (cost * self.markup).quantize(Decimal("0.000001"))
        keys: set[str] = set()
        for item in root_revisions:
            pending_row = outbox.get((item.observation_id, item.revision))
            if (
                pending_row is None
                or not pending_row.settled
                or pending_row.row.observation != item.observation
            ):
                return pending("SETTLEMENT_PENDING")
            keys.add(
                f"turn:{self.session.id}:{item.observation_id}"
                if item.revision == 1
                else f"adjust:{self.binding.id}:{item.observation_id}:{item.revision}"
            )
        entries = [v for v in rows.ledger if v.idempotency_key in keys]
        if not entries or any(
            v.reason != "turn_debit" or v.channel_id != self.binding.thread.channel.channel_id
            for v in entries
        ):
            return pending("LEDGER_UNKNOWN")
        with localcontext() as arithmetic:
            arithmetic.prec = 80
            if -sum((v.delta_usd for v in entries), Decimal(0)) != expected:
                return pending("LEDGER_COST_MISMATCH")
        return NativeFooterCost("complete", "SETTLED_NATIVE_ROOT", expected)

    def display(self, totals: object) -> str:
        # usage_totals imports turn.__init__, which imports this billing type.
        # Resolve the rendering subtype only after module initialization.
        from daimon.core.usage_totals import ProviderUsageTotals

        if self._invalid is not None:
            return "Cost unknown"
        if not isinstance(totals, ProviderUsageTotals) or any(
            v.session != self.session or v.turn_id != self.root_turn_id for v in totals.observations
        ):
            return "Cost unknown"
        # A snapshot added after finalization cannot inherit an older numeric cost.
        latest: tuple[UsageObservation, ...] = ()
        for value in totals.observations:
            latest = replace_observation(latest, value)
        if self._walk is not None and {(v.id, v.revision) for v in latest} != {
            (v.id, v.revision) for v in self._walk
        }:
            return "Cost partial"
        if self._walk is not None:
            excluded = {"observed_at", "native_revision"}
            expected = {v.id: v.model_dump(exclude=excluded) for v in self._walk}
            if any(v.model_dump(exclude=excluded) != expected[v.id] for v in latest):
                return "Cost partial"
        return self.result.display


def native_footer_text(totals: object) -> str | None:
    """No-op outside the host's explicitly admitted native attempt context."""
    current = _CURRENT.get()
    return current.display(totals) if current is not None else None
