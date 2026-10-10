"""Host usage accounting composed in the caller's AsyncSession transaction.

The default M0 live recorder remains unchanged. Neutral backends use
record_observation_usage, or apply_usage_outbox to drain durable pending rows
after a restart. Callers bind the event's tenant, attribution, rates and markup
and billing grain, retaining that context across revisions. Nothing here commits
a transaction.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, localcontext
from typing import Literal

from daimon.core.pricing import (
    ModelRates,
    ProviderPrice,
    provider_cost_of,
    provider_usage_tokens,
    uncached_input_tokens,
    usage_tokens,
)
from daimon.core.stores import accounting_outbox, mux_state, tenant_ledger, usage_events
from daimon.core.tenant_balance import debit_amount
from daimon.core.usage_recording import TurnLedgerReason
from mux.contracts.usage import UsageObservation
from mux.errors import ScopeViolation
from mux.state.usage_ledger import AppliedUsage, OutboxRow, UsageRevisionConflict
from sqlalchemy.ext.asyncio import AsyncSession

type BillingGrain = Literal["model_request", "turn", "session"]


def _effective(applied: AppliedUsage) -> UsageObservation:
    return applied.observation.model_copy(update=dict(applied.tokens))


def _amount(applied: AppliedUsage | None, rates: ModelRates | None, markup: Decimal) -> Decimal:
    if applied is None or rates is None:
        return Decimal("0.000000")
    observation = _effective(applied)
    uncached = uncached_input_tokens(observation)
    # Unknown stages stay unknown in the durable snapshots. Price each known
    # disjoint stage once; inclusive input cannot price uncached units until
    # both cache subsets are known. Keep the legacy float operation order.
    cost = (
        (uncached * rates.input / 1_000_000 if uncached is not None else 0.0)
        + (
            observation.output_tokens * rates.output / 1_000_000
            if observation.output_tokens is not None
            else 0.0
        )
        + (
            observation.input_cache_write_tokens * rates.cache_write / 1_000_000
            if observation.input_cache_write_tokens is not None
            else 0.0
        )
        + (
            observation.input_cached_tokens * rates.cache_read / 1_000_000
            if observation.input_cached_tokens is not None
            else 0.0
        )
    )
    return debit_amount(cost, markup=markup)


async def apply_usage_outbox(
    session: AsyncSession,
    row: OutboxRow,
    *,
    tenant_id: uuid.UUID | None,
    platform_user_id: str | None,
    pricing: ModelRates | None,
    markup: Decimal = Decimal("1"),
    reason: TurnLedgerReason = "turn_debit",
    channel_id: str | None = None,
    billing_grain: BillingGrain = "model_request",
    provider_price: ProviderPrice | None = None,
    infrastructure_usd: Decimal | None = None,
) -> bool:
    """Claim, project and debit one durable revision atomically. True if claimed.

    The caller selects one billing grain, retaining it across revisions.
    Covered aggregates remain durable without charging their covered leaves
    again. Unresolved coverage and a mismatched grain fail before claiming.
    DM exemption is host-owned. A rollback reopens the outbox claim along with
    rolling back both host writes, so a restarted worker can apply it once.
    """
    if tenant_id is None:
        return False
    if not session.in_transaction():
        raise ValueError("accounting requires a caller-owned transaction")
    revisions = await accounting_outbox.load_revisions(session, row, tenant_id=tenant_id)
    if row.observation.session.provider != "anthropic":
        await accounting_outbox.check_provider_ingest(
            session, row.binding_id, row.observation, tenant_id=tenant_id
        )
    observation = revisions.current.observation
    if observation.session.kind != "session":
        raise ValueError("usage requires a session reference")
    if observation.session.tenant_id not in (None, str(tenant_id)):
        raise ScopeViolation(observation.id, "usage observation belongs to another tenant")
    if observation.covers:
        if not await accounting_outbox.check_covered_usage(
            session,
            row,
            tenant_id=tenant_id,
            billing_grain=billing_grain,
            require_settled=observation.session.provider != "anthropic",
        ):
            return False
    elif observation.grain != billing_grain:
        raise ValueError(
            "usage requires the caller's configured billing grain or explicit coverage"
        )
    current = _effective(revisions.current)
    exact_amount: Decimal | None = None
    first_exact_settlement = False
    if not observation.covers and observation.session.provider != "anthropic":
        if provider_price is None:
            return False  # persist unknown pricing for post-run reconciliation
        latest, settled = await accounting_outbox.settled_amount(
            session, row, tenant_id=tenant_id, channel_id=channel_id
        )
        first_exact_settlement = latest == 0
        if latest > observation.revision:
            return await mux_state.mark_outbox_applied(session, row)
        actual = provider_cost_of(
            observation, provider_price, infrastructure_usd=infrastructure_usd
        )
        if actual is None:
            return False  # durable pending, not a zero charge or released hold
        await accounting_outbox.check_provider_grain(session, row, tenant_id=tenant_id)
        with localcontext() as context:
            context.prec = 80
            exact_amount = (actual * markup).quantize(Decimal("0.000001")) - settled
    if not await mux_state.mark_outbox_applied(session, row):
        return False
    if observation.covers:
        return True
    if observation.model is None:
        raise ValueError("a billable usage observation requires a model")
    projection = observation if observation.session.provider != "anthropic" else current
    await usage_events.project_revision(
        session,
        tenant_id=tenant_id,
        platform_user_id=platform_user_id,
        observation=projection,
        initial=revisions.prior is None,
        channel_id=channel_id,
        projected_tokens=provider_usage_tokens(projection, provider_price)
        if provider_price is not None
        else None,
    )
    amount = (
        exact_amount
        if exact_amount is not None
        else (
            _amount(revisions.current, pricing, markup) - _amount(revisions.prior, pricing, markup)
        )
    )
    key = (
        f"turn:{observation.session.id}:{observation.id}"
        if row.revision == 1
        else f"adjust:{row.binding_id}:{row.observation_id}:{row.revision}"
    )
    await accounting_outbox.check_ledger_key(
        session,
        key=key,
        tenant_id=tenant_id,
        channel_id=channel_id,
        allow_legacy=row.revision == 1,
    )
    if (
        amount != 0
        or first_exact_settlement
        or (row.revision == 1 and usage_tokens(current) is not None)
    ):
        await tenant_ledger.insert_entry(
            session,
            tenant_id=tenant_id,
            delta_usd=-amount,
            reason=reason,
            idempotency_key=key,
            channel_id=channel_id,
            occurred_at=revisions.occurred_at,
        )
    return True


async def record_observation_usage(
    session: AsyncSession,
    *,
    binding_id: str,
    observation: UsageObservation,
    tenant_id: uuid.UUID | None,
    platform_user_id: str | None,
    pricing: ModelRates | None,
    markup: Decimal = Decimal("1"),
    reason: TurnLedgerReason = "turn_debit",
    channel_id: str | None = None,
    billing_grain: BillingGrain = "model_request",
    provider_price: ProviderPrice | None = None,
    infrastructure_usd: Decimal | None = None,
) -> bool:
    """Record neutral usage and apply its outbox in the same host transaction."""
    if tenant_id is None:
        return False
    if not session.in_transaction():
        raise ValueError("accounting requires a caller-owned transaction")
    if observation.session.provider != "anthropic":
        await accounting_outbox.check_provider_ingest(
            session, binding_id, observation, tenant_id=tenant_id
        )
    row = await mux_state.record_usage(session, binding_id, observation)
    if row is None and observation.session.provider != "anthropic":
        row = await accounting_outbox.pending_revision(
            session, binding_id, observation.id, observation.revision
        )
        if row is not None and row.observation.model_dump(
            exclude={"observed_at", "native_revision"}
        ) != observation.model_dump(exclude={"observed_at", "native_revision"}):
            raise UsageRevisionConflict(observation.id, observation.revision)
    if row is None:
        return False
    return await apply_usage_outbox(
        session,
        row,
        tenant_id=tenant_id,
        platform_user_id=platform_user_id,
        pricing=pricing,
        markup=markup,
        reason=reason,
        channel_id=channel_id,
        billing_grain=billing_grain,
        provider_price=provider_price,
        infrastructure_usd=infrastructure_usd,
    )
