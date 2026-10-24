"""Selfhosted billing with durable tariff admission and immutable receipt context."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import cast

from daimon.core.accounting_outbox import record_observation_usage
from daimon.core.config import SelfhostedBillingSettings, SelfhostedModelPrice
from daimon.core.pricing import ProviderPrice, provider_cost_of
from daimon.core.stores import selfhosted_usage as contexts
from daimon.core.turn.errors import AdmissionDenied
from mux.contracts.ids import ModelRef, ResourceRef
from mux.contracts.usage import UsageObservation
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class _SelfhostedBillingIdentity(BaseModel):
    """Accounting identity can be recovered independently of damaged rates."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    binding_id: str
    session: ResourceRef
    deployment: str
    underlying_model: str


class SelfhostedTariffAdmission(_SelfhostedBillingIdentity):
    """Host-owned snapshot committed before paid work, separate from native meters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    admission_key: str
    tariff_effective_from: date
    tariff: SelfhostedModelPrice
    admitted_at: datetime
    admission_clock_skew_s: float = Field(default=5, ge=0, le=60, allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def restore_saved_identity(cls, value: object) -> object:
        # Older snapshots already contain this date in their frozen tariff.
        # Recover only from that evidence, never from current configuration.
        if isinstance(value, dict):
            saved = cast(dict[str, object], value)
            tariff = saved.get("tariff")
            if "tariff_effective_from" not in saved and isinstance(tariff, dict):
                rates = cast(dict[str, object], tariff)
                return {**saved, "tariff_effective_from": rates.get("effective_from")}
            return saved
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> SelfhostedTariffAdmission:
        if self.tariff_effective_from != self.tariff.effective_from:
            raise ValueError("admitted tariff identity differs from its rates")
        return self

    def price(self) -> ProviderPrice:
        # Validity is checked at admission. A response/correction can arrive
        # after a newer tariff takes effect, without changing its admitted cost.
        return ProviderPrice(
            provider="selfhosted",
            model=self.underlying_model,
            checked_on=self.tariff_effective_from,
            admitted_on=self.admitted_at.date(),
            input=self.tariff.input,
            output=self.tariff.output,
            cache_read=self.tariff.cache_read,
        )


def _rate(settings: SelfhostedBillingSettings, model: str, on: date) -> SelfhostedModelPrice | None:
    history = sorted(settings.models.get(model, ()), key=lambda row: row.effective_from)
    eligible = [row for row in history if row.effective_from <= on]
    if not eligible:
        return None
    row = eligible[-1]
    return row if row.valid_before is None or on < row.valid_before else None


def selfhosted_prices(
    settings: SelfhostedBillingSettings, *, on: date | None = None
) -> Mapping[tuple[str, str], ProviderPrice]:
    """Current previews only; never use moving configuration for settlement."""
    settings = SelfhostedBillingSettings.model_validate(settings.model_dump())
    on = on or datetime.now(UTC).date()
    identities = {model: model for model in settings.models} | settings.deployments
    prices: dict[tuple[str, str], ProviderPrice] = {}
    for identity, model in identities.items():
        row = _rate(settings, model, on)
        if row is not None:
            prices[("selfhosted", identity)] = ProviderPrice(
                provider="selfhosted",
                model=identity,
                checked_on=row.effective_from,
                input=row.input,
                output=row.output,
                cache_read=row.cache_read,
                valid_before=row.valid_before,
            )
    return MappingProxyType(prices)


def require_selfhosted_price(settings: SelfhostedBillingSettings, deployment: str) -> ProviderPrice:
    """Gate the runtime must call before paid work; this alone persists nothing."""
    try:
        price = selfhosted_prices(settings).get(("selfhosted", deployment))
        if deployment in settings.deployments and price is not None:
            return price
    except ValueError:
        pass
    raise AdmissionDenied(reason="backend_unsupported")


async def admit_selfhosted_usage(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    binding_id: str,
    session_ref: ResourceRef,
    admission_key: str,
    deployment: str,
    billing: SelfhostedBillingSettings,
) -> SelfhostedTariffAdmission:
    """Commit one turn's tariff before any model I/O; retries return its snapshot.

    admission_key is the durable input/send idempotency key. All requests in
    that admitted turn retain this context. Never generate a fresh key during
    reconcile. Configuration changes cannot replace a saved admission.
    """
    if not admission_key:
        raise ValueError("billing admission requires a durable key")
    key = contexts.context_key("admission", binding_id, admission_key)
    async with sessionmaker() as db, db.begin():
        await contexts.check_session(
            db, tenant_id=tenant_id, binding_id=binding_id, native=session_ref
        )
        prior = await contexts.read_context(db, tenant_id=tenant_id, binding_id=binding_id, key=key)
        if prior is None:
            require_selfhosted_price(billing, deployment)
            now = datetime.now(UTC)
            model = billing.deployments[deployment]
            rate = _rate(billing, model, now.date())
            if rate is None:
                raise AdmissionDenied(reason="backend_unsupported")
            admitted = SelfhostedTariffAdmission(
                admission_key=admission_key,
                binding_id=binding_id,
                session=session_ref,
                deployment=deployment,
                underlying_model=model,
                tariff_effective_from=rate.effective_from,
                tariff=rate,
                admitted_at=now,
                admission_clock_skew_s=billing.admission_clock_skew_s,
            )
            prior = await contexts.create_context(
                db,
                tenant_id=tenant_id,
                binding_id=binding_id,
                key=key,
                value=admitted.model_dump(mode="json"),
            )
        result = SelfhostedTariffAdmission.model_validate(prior)
        if (result.binding_id, result.session, result.deployment, result.admission_key) != (
            binding_id,
            session_ref,
            deployment,
            admission_key,
        ):
            raise ValueError("billing admission key belongs to another request")
        return result


async def record_selfhosted_usage(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    binding_id: str,
    observation: UsageObservation,
    tenant_id: uuid.UUID,
    platform_user_id: str | None,
    infrastructure_usd: Decimal | None,
    admission_key: str | None = None,
    markup: Decimal = Decimal("1"),
    channel_id: str | None = None,
) -> bool:
    """Record receipts using only durable admission, never current configuration.

    The first valid receipt association freezes the tariff for all corrections;
    an unassociated receipt may adopt an existing valid admission during replay.
    Absent/corrupt pricing context leaves a durable pending reason. Accounting
    uses the admitted underlying model; source aliases are saved separately
    and native meters stay untouched. infrastructure_usd is required: None is
    unknown, explicit audited zero means the operator funds compute separately.
    """
    from daimon.core.turn.outcomes import current_outcome

    if (
        observation.session.provider != "selfhosted"
        or observation.grain != "model_request"
        or observation.basis != "increment"
        or observation.covers
        or observation.thread_id is not None
    ):
        raise ValueError("selfhosted billing requires incremental model-request usage")
    receipt_key = contexts.context_key("receipt", binding_id, observation.id)
    billing_observation = observation
    price: ProviderPrice | None = None
    reason: str | None = None
    async with sessionmaker() as db, db.begin():
        await contexts.check_session(
            db, tenant_id=tenant_id, binding_id=binding_id, native=observation.session
        )
        stored_model = await contexts.recorded_model(
            db, tenant_id=tenant_id, binding_id=binding_id, observation_id=observation.id
        )
        if stored_model is not None:
            # A damaged tariff must not send a deployment alias back into an
            # accounting stream already normalised to the underlying model.
            billing_observation = observation.model_copy(update={"model": stored_model})
        try:
            original = await contexts.read_context(
                db, tenant_id=tenant_id, binding_id=binding_id, key=receipt_key, lock=True
            )
            if original is None:
                original = await contexts.create_context(
                    db,
                    tenant_id=tenant_id,
                    binding_id=binding_id,
                    key=receipt_key,
                    value={"admission": None},
                )
            if (
                original.get("admission") is None
                and original.get("invalid_context") is None
                and admission_key is not None
            ):
                candidate = await contexts.read_context(
                    db,
                    tenant_id=tenant_id,
                    binding_id=binding_id,
                    key=contexts.context_key("admission", binding_id, admission_key),
                )
                if candidate is not None:
                    admitted = SelfhostedTariffAdmission.model_validate(candidate)
                    if admitted.binding_id != binding_id or admitted.session != observation.session:
                        reason = "tariff_context_invalid"
                    elif admitted.admitted_at > observation.observed_at + timedelta(
                        seconds=admitted.admission_clock_skew_s
                    ):
                        reason = "admission_after_request"
                    elif observation.model is not None and (
                        observation.model.provider != "selfhosted"
                        or observation.model.id
                        not in (admitted.deployment, admitted.underlying_model)
                    ):
                        reason = "model_mismatch"
                    else:
                        original = await contexts.adopt_admission(
                            db,
                            tenant_id=tenant_id,
                            binding_id=binding_id,
                            key=receipt_key,
                            observation_id=observation.id,
                            admission=candidate,
                            deployment=admitted.deployment,
                            model=ModelRef(provider="selfhosted", id=admitted.underlying_model),
                            earliest_observed_at=admitted.admitted_at
                            - timedelta(seconds=admitted.admission_clock_skew_s),
                        )
                        stored_model = ModelRef(provider="selfhosted", id=admitted.underlying_model)
            if original.get("invalid_context") is not None:
                raise ValueError("receipt pricing context is corrupt")
            snapshot = original.get("admission")
            if snapshot is None:
                reason = reason or "admission_missing"
            else:
                identity = _SelfhostedBillingIdentity.model_validate(snapshot)
                if (
                    stored_model is None
                    and identity.binding_id == binding_id
                    and identity.session == observation.session
                    and (
                        observation.model is None
                        or observation.model.provider == "selfhosted"
                        and observation.model.id in (identity.deployment, identity.underlying_model)
                    )
                ):
                    billing_observation = observation.model_copy(
                        update={
                            "model": ModelRef(provider="selfhosted", id=identity.underlying_model)
                        }
                    )
                frozen = SelfhostedTariffAdmission.model_validate(snapshot)
                if (
                    frozen.binding_id != binding_id
                    or frozen.session != observation.session
                    or stored_model is not None
                    and stored_model != ModelRef(provider="selfhosted", id=frozen.underlying_model)
                ):
                    reason = "tariff_context_invalid"
                elif frozen.admitted_at > observation.observed_at + timedelta(
                    seconds=frozen.admission_clock_skew_s
                ):
                    reason = "admission_after_request"
                elif observation.model is None or (
                    observation.model.provider == "selfhosted"
                    and observation.model.id in (frozen.deployment, frozen.underlying_model)
                ):
                    # The admitted model is the accounting identity even when
                    # replay changes between deployment and model aliases.
                    # Keep the source alias separately; never alter native meters.
                    billing_observation = observation.model_copy(
                        update={
                            "model": ModelRef(provider="selfhosted", id=frozen.underlying_model)
                        }
                    )
                    if observation.model is None:
                        reason = "model_unknown"
                    else:
                        price = frozen.price()
                else:
                    reason = "model_mismatch"
        except (ValueError, ArithmeticError):
            reason = "tariff_context_invalid"
        if observation.completeness != "measured":
            reason = f"usage_{observation.completeness}"
        elif reason is None and infrastructure_usd is None:
            reason = "infrastructure_unknown"
        elif reason is None and price is not None:
            try:
                if (
                    provider_cost_of(
                        billing_observation, price, infrastructure_usd=infrastructure_usd
                    )
                    is None
                ):
                    reason = "usage_counts_missing"
            except (ValueError, ArithmeticError):
                reason = "pricing_invalid"
                price = None
        if reason is not None:
            price = None
        settled = await record_observation_usage(
            db,
            binding_id=binding_id,
            observation=billing_observation,
            tenant_id=tenant_id,
            platform_user_id=platform_user_id,
            pricing=None,
            provider_price=price,
            infrastructure_usd=infrastructure_usd,
            billing_grain="model_request",
            markup=markup,
            channel_id=channel_id,
        )
        # Even an invalid snapshot must not prevent the native receipt write.
        # Database failures still propagate and roll back the entire transaction.
        await contexts.note_pending_reason(
            db,
            tenant_id=tenant_id,
            binding_id=binding_id,
            key=receipt_key,
            revision=observation.revision,
            reason=reason,
            native_model=observation.model,
        )
    if (outcome := current_outcome.get()) is not None:
        outcome.note_usage(
            billing_observation,
            metered=True,
            provider_price=price,
            infrastructure_usd=infrastructure_usd,
        )
    return settled
