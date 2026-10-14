"""Owned offline host turns and independent channel-budget readback.

Controlled rates prove host arithmetic only. Callers supply an already prepared
host turn and isolated offline transports; no provider client is discovered.
Only the production usage recorders write debits. Snapshots never write.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import cast

from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.core._models import ProviderBindingSlot
from daimon.core._models import UsageObservation as StoredUsageObservation
from daimon.core.channel_budget import ChannelBudgetStatus, get_channel_budget_status
from daimon.core.pricing import ModelRates, ProviderPrice
from daimon.core.stores import mux_state, tenant_ledger, usage_events
from daimon.core.stores.domain import TenantLedgerRow, UsageEventRow
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.prepare import PreparedTurn
from daimon.core.turn.run import RunOutcome, run_prepared_turn
from daimon.core.turn.termination import TerminationReason
from daimon.core.usage_recording import record_provider_usage, record_turn_usage
from daimon.core.usage_totals import ProviderUsageTotals
from daimon.testing.db import require_owned_sessionmaker
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.state.store import binding_slot
from mux.state.usage_ledger import AppliedUsage
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class ControlledTurnPricing:
    """Explicit MOCK prices; never a live rate or usage certificate.

    None infrastructure for a native provider retains pending usage. Explicit
    zero is permitted only as the caller's documented offline test policy.
    """

    evidence_id: str
    model_id: str
    legacy_rates: ModelRates | None = None
    provider_price: ProviderPrice | None = None
    infrastructure_usd: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.evidence_id.strip() or not self.model_id.strip():
            raise ValueError("controlled prices require model and mock policy evidence")
        if (self.legacy_rates is None) == (self.provider_price is None):
            raise ValueError("supply exactly one legacy or provider mock tariff")
        if self.legacy_rates is not None and any(
            not math.isfinite(value) or value < 0
            for value in (
                self.legacy_rates.input,
                self.legacy_rates.output,
                self.legacy_rates.cache_read,
                self.legacy_rates.cache_write,
            )
        ):
            raise ValueError("mock rates must be finite and nonnegative")
        if self.provider_price is not None and self.provider_price.model != self.model_id:
            raise ValueError("mock tariff differs from the bound model")
        if self.infrastructure_usd is not None and (
            not self.infrastructure_usd.is_finite() or self.infrastructure_usd < 0
        ):
            raise ValueError("mock infrastructure must be finite and nonnegative")


@dataclass(frozen=True)
class ChannelAccountingSnapshot:
    tenant_id: uuid.UUID
    platform: str
    channel_id: str
    observed_at: datetime
    entries: tuple[TenantLedgerRow, ...]
    usage: tuple[UsageEventRow, ...]
    net_debit_usd: Decimal
    budget: ChannelBudgetStatus | None
    native_usage: tuple[AppliedUsage, ...] = ()

    @property
    def debits(self) -> tuple[TenantLedgerRow, ...]:
        return tuple(row for row in self.entries if row.delta_usd < 0)


@dataclass(frozen=True)
class MeteredChannelTurnReceipt:
    outcome: RunOutcome
    before: ChannelAccountingSnapshot
    after: ChannelAccountingSnapshot
    new_debits: tuple[TenantLedgerRow, ...]
    pricing: ControlledTurnPricing
    issues: tuple[str, ...]
    source: str = "controlled_offline_host_billing"

    @property
    def metered(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class ChannelAccounting:
    sessions: async_sessionmaker[AsyncSession]
    scope: Scope
    platform: str
    channel_id: str

    def __post_init__(self) -> None:
        self._admit()

    def _admit(self) -> None:
        require_owned_sessionmaker(self.sessions)
        kwargs = cast(dict[str, object], self.sessions.kw)
        routed = kwargs.get("binds")
        if isinstance(routed, Mapping) and any(
            value is not kwargs.get("bind")
            for value in cast(Mapping[object, object], routed).values()
        ):
            raise ValueError("channel accounting requires one consistent database bind")
        uuid.UUID(self.scope.tenant_id)
        uuid.UUID(self.scope.account_id)
        if (
            self.scope.is_platform
            or self.scope.is_legacy_host_authorized
            or self.platform not in ("discord", "slack", "teams")
            or not self.channel_id.strip()
        ):
            raise ValueError("channel accounting requires explicit tenant/channel authorization")

    async def snapshot(self, *, now: datetime | None = None) -> ChannelAccountingSnapshot:
        """Independent coherent SELECTs, with the production budget window/policy."""
        self._admit()
        instant = now if now is not None else datetime.now(UTC)
        if instant.utcoffset() is None:
            raise ValueError("channel accounting time must be timezone aware")
        tenant = uuid.UUID(self.scope.tenant_id)
        async with self.sessions() as db, db.begin():
            await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            with db.no_autoflush, localcontext() as context:
                context.prec = 80
                entries = tuple(
                    row
                    for row in await tenant_ledger.list_for_tenant(db, tenant_id=tenant)
                    if row.channel_id == self.channel_id
                )
                usage = tuple(
                    row
                    for row in await usage_events.list_for_tenant(db, tenant_id=tenant)
                    if row.channel_id == self.channel_id
                )
                spent = await tenant_ledger.get_channel_spend(
                    db, tenant_id=tenant, channel_id=self.channel_id, since=None, until=None
                )
                budget = await get_channel_budget_status(
                    db,
                    tenant_id=tenant,
                    platform=self.platform,
                    channel_id=self.channel_id,
                    now=instant,
                )
                native_usage = tuple(
                    AppliedUsage.model_validate(body)
                    for body in await db.scalars(
                        select(StoredUsageObservation.applied)
                        .join(
                            ProviderBindingSlot,
                            ProviderBindingSlot.binding_id == StoredUsageObservation.binding_id,
                        )
                        .where(
                            StoredUsageObservation.tenant_id == tenant,
                            ProviderBindingSlot.tenant_id == tenant,
                            ProviderBindingSlot.platform == self.platform,
                            ProviderBindingSlot.channel_id == self.channel_id,
                        )
                    )
                )
        return ChannelAccountingSnapshot(
            tenant,
            self.platform,
            self.channel_id,
            instant,
            entries,
            usage,
            spent,
            budget,
            native_usage,
        )

    async def run_turn(
        self,
        *,
        deps: TurnDeps,
        prepared: PreparedTurn,
        pricing: ControlledTurnPricing,
        thread_id: str,
        external_user_id: str,
        user_message: str,
        lifecycle: TurnLifecycle,
        cancel: asyncio.Event,
        operation_key: str,
        binding: ProviderBinding | None = None,
    ) -> MeteredChannelTurnReceipt:
        """Run the REAL host driver with an explicit production billing recorder.

        Preparation/admission and isolated provider transports are caller-owned.
        This seam supplies only mock tariff context; it never inserts usage or
        ledger rows itself, bypasses admission, or certifies the next denial.
        """
        self._admit()
        recorded_legacy_ids: set[str] = set()
        recorded_native: list[UsageObservation] = []
        if (
            deps.sessionmaker is not self.sessions
            or str(prepared.session_account_id) != self.scope.account_id
            or prepared.admission.budget_channel_id != self.channel_id
            or not thread_id.strip()
            or not external_user_id.strip()
            or not operation_key.strip()
        ):
            raise ValueError("metered turn differs from the owned channel/account/DB context")
        tenant = uuid.UUID(self.scope.tenant_id)
        ref = prepared.session_ref
        if ref is not None and (
            ref.kind != "session"
            or ref.id != prepared.ma_session_id
            or ref.tenant_id != self.scope.tenant_id
            or ref.account_id != self.scope.account_id
        ):
            raise ValueError("metered turn requires the authorized prepared session")
        provider = ref.provider if ref is not None else "anthropic"
        if provider == "anthropic":
            if (
                pricing.legacy_rates is None
                or prepared.admission.agent.model.id != pricing.model_id
                or pricing.infrastructure_usd not in (None, Decimal(0))
            ):
                raise ValueError("legacy mock prices differ from the prepared model")

            async def record_legacy(*, event: BetaManagedAgentsSpanModelRequestEndEvent) -> None:
                self._admit()
                await record_turn_usage(
                    sessionmaker=self.sessions,
                    tenant_id=tenant,
                    platform_user_id=external_user_id,
                    managed_session_id=prepared.ma_session_id,
                    model_id=pricing.model_id,
                    event=event,
                    pricing=pricing.legacy_rates,
                    markup=deps.markup,
                    channel_id=self.channel_id,
                )
                recorded_legacy_ids.add(event.id)

            billed = replace(prepared, _record=record_legacy)
        else:
            price = pricing.provider_price
            revision = prepared.admission.backend_revision
            if (
                ref is None
                or price is None
                or binding is None
                or revision is None
                or price.provider != provider
                or revision.model != pricing.model_id
                or revision.channel.tenant_id != self.scope.tenant_id
                or revision.channel.platform != self.platform
                or revision.channel.channel_id != self.channel_id
                or binding.thread.channel != revision.channel
                or binding.thread.thread_id != thread_id
                or binding.native_refs.get("session") != ref.id
                or binding.provider != provider
                or binding.profile != revision.profile
                or binding.config_revision != revision.local
                or binding.legacy_account_id != self.scope.account_id
                or ref.id != prepared.ma_session_id
                or ref.tenant_id != self.scope.tenant_id
                or ref.account_id != self.scope.account_id
            ):
                raise ValueError("native mock prices require the admitted binding/session/model")
            async with self.sessions() as db, db.begin():
                await db.execute(text("SET TRANSACTION READ ONLY"))
                if await mux_state.get_binding(db, binding_slot(binding)) != binding:
                    raise ValueError("native metered turn binding is not current")

            async def record_native(*, observation: UsageObservation) -> bool:
                self._admit()
                if observation.session != ref or observation.thread_id is not None:
                    raise ValueError("bounded metered turn requires native root usage")
                settled = await record_provider_usage(
                    sessionmaker=self.sessions,
                    binding_id=binding.id,
                    observation=observation,
                    tenant_id=tenant,
                    platform_user_id=external_user_id,
                    provider_price=price,
                    infrastructure_usd=pricing.infrastructure_usd,
                    billing_grain="turn",
                    managed_session_id=prepared.ma_session_id,
                    model_id=pricing.model_id,
                    markup=deps.markup,
                    channel_id=self.channel_id,
                )
                recorded_native.append(observation)
                return settled

            billed = replace(prepared, _record=record_native)
        before = await self.snapshot()
        self._admit()

        async def reseed() -> str:
            return user_message

        outcome = await run_prepared_turn(
            deps,
            billed,
            tenant_id=tenant,
            platform=self.platform,
            thread_id=thread_id,
            external_user_id=external_user_id,
            user_message=user_message,
            lifecycle=lifecycle,
            cancel=cancel,
            reseed_user_message=reseed,
            recovery_lifecycle=lambda _: lifecycle,
            operation_key=operation_key,
            render_interval_s=0.01,
        )
        after = await self.snapshot()
        prior_keys = {row.idempotency_key for row in before.entries}
        new = tuple(row for row in after.debits if row.idempotency_key not in prior_keys)
        issues: list[str] = []
        if (
            outcome.state.error is not None
            or outcome.recovered
            or outcome.state.termination != TerminationReason.COMPLETED
        ):
            issues.append("HOST_TURN_NOT_SUCCESSFUL_SINGLE_ATTEMPT")
        if len(new) != 1:
            issues.append("EXPECTED_ONE_NEW_CHANNEL_DEBIT")
        observed_ids = recorded_legacy_ids & outcome.state.seen_event_ids
        native = outcome.state.usage_totals
        expected_revision: dict[str, int | None] = dict.fromkeys(observed_ids, None)
        if provider != "anthropic":
            selected = native.selected if isinstance(native, ProviderUsageTotals) else ()
            if (
                len(selected) != 1
                or selected[0].turn_id is None
                or selected[0].thread_id is not None
                or selected[0] not in recorded_native
            ):
                issues.append("AMBIGUOUS_OR_MISSING_RUN_USAGE_IDENTITY")
                expected_revision = {}
            else:
                observed = selected[0]
                attributed = observed.model_copy(
                    update={
                        "model": observed.model or ModelRef(provider=provider, id=pricing.model_id)
                    }
                )
                persisted = tuple(
                    value
                    for value in after.native_usage
                    if binding is not None
                    and value.binding_id == binding.id
                    and value.observation_id == observed.id
                    and value.revision == observed.revision
                    and value.observation == attributed
                )
                expected_revision = {observed.id: observed.revision} if len(persisted) == 1 else {}
                if not expected_revision:
                    issues.append("PERSISTED_RUN_USAGE_IDENTITY_MISMATCH")
        projected = {
            f"turn:{row.managed_session_id}:{row.event_id}"
            for row in after.usage
            if row.managed_session_id == outcome.ma_session_id
            and row.platform_user_id == external_user_id
            and row.model == pricing.model_id
            and row.event_id in expected_revision
            and row.observation_revision == expected_revision[row.event_id]
        }
        if any(row.reason != "turn_debit" or row.idempotency_key not in projected for row in new):
            issues.append("DEBIT_USAGE_IDENTITY_MISMATCH")
        return MeteredChannelTurnReceipt(outcome, before, after, new, pricing, tuple(issues))
