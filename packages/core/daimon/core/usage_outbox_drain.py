"""Settle durable pending usage of profiles whose infrastructure is known to be $0.

Usage recorded while a profile's infrastructure cost was unknown stayed pending in the
accounting outbox (SMOKE fc0fa16a: 156 of 156 rows on staging since 10-15), so it was
never debited and the fleet runs it belonged to never settled. Once a profile is known
to have no infrastructure (infrastructure_cost.NO_INFRASTRUCTURE_PROFILES) this drain
applies those rows through the same apply_usage_outbox the live path uses.

Idempotent: apply_usage_outbox claims a row in the transaction that writes its ledger
debit, so an applied row is never applied again. Bounded: one batch per pass, newest
revision of each observation first (a settled revision closes its older ones). Rows it
cannot price (unknown tariff, partial counts, another profile) stay pending.
"""

from decimal import Decimal

import structlog
from daimon.core.accounting_outbox import apply_usage_outbox
from daimon.core.infrastructure_cost import NO_INFRASTRUCTURE_PROFILES, known_infrastructure_usd
from daimon.core.pricing import provider_cost_of, provider_prices
from daimon.core.stores import mux_state
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: Rows one pass looks at. The next scheduler tick continues.
DRAIN_BATCH = 200


async def drain_pending_usage(
    sessionmaker: async_sessionmaker[AsyncSession], *, markup: Decimal, limit: int = DRAIN_BATCH
) -> tuple[int, Decimal]:
    """Apply what pending usage can now be priced; (rows applied, USD debited)."""
    async with sessionmaker() as db:
        rows = await mux_state.pending_outbox_for_profiles(
            db, NO_INFRASTRUCTURE_PROFILES, limit=limit
        )
    applied, debited, skipped = 0, Decimal(0), 0
    # An observation's newest revision carries its whole cost; older ones only close.
    counted: set[tuple[str, str]] = set()
    for row, tenant_id, binding in rows:
        observation = row.observation
        infrastructure = known_infrastructure_usd(binding.profile)
        price = (
            provider_prices(binding.profile).get(
                (observation.session.provider, observation.model.id)
            )
            if observation.model is not None
            else None
        )
        if infrastructure is None or price is None:
            skipped += 1
            continue
        try:
            async with sessionmaker.begin() as db:
                claimed = await apply_usage_outbox(
                    db,
                    row,
                    tenant_id=tenant_id,
                    platform_user_id=None,
                    pricing=None,
                    provider_price=price,
                    infrastructure_usd=infrastructure,
                    billing_grain=observation.grain,
                    markup=markup,
                    channel_id=binding.thread.channel.channel_id,
                )
        except Exception as error:  # one bad row must not stop the rest
            log.warning(
                "accounting.pending_usage_row_failed",
                binding_id=row.binding_id,
                revision=row.revision,
                error_class=type(error).__name__,
            )
            continue
        if claimed:
            applied += 1
            key = (row.binding_id, row.observation_id)
            cost = provider_cost_of(observation, price, infrastructure_usd=infrastructure)
            if cost is not None and key not in counted:
                counted.add(key)
                debited += (cost * markup).quantize(Decimal("0.000001"))
    if rows:
        log.info(
            "accounting.pending_usage_drained",
            seen=len(rows),
            applied=applied,
            skipped=skipped,
            usd=str(debited),
        )
    return applied, debited
