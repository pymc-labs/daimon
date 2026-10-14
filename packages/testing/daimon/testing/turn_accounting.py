"""Explicit read-only turn accounting for owned offline databases.

Persisted revisions are facts. Rates, infrastructure and native inventory seals
are independent caller evidence; settlement alone does not prove those facts.
No reader seals a native walk, creates an aggregate or certifies live spend.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Literal, cast

from daimon.core.pricing import ModelRates, ProviderPrice, cost_of, provider_cost_of
from daimon.core.stores.turn_usage import UsageAccountingRows, read_accounting_revisions
from daimon.core.tenant_balance import debit_amount
from daimon.core.usage_aggregation import disjoint_observations, replace_observation
from daimon.testing.db import require_owned_sessionmaker
from mux.contracts.ids import ModelRef, ResourceRef, Scope
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class TurnUsageInventory:
    """Independent terminal native inventory, or an exhausted request walk.

    Capture this from the authorized native execution path, never by enumerating
    the database being checked. Revisions include covered and delegated work.
    The evidence must establish membership, disjointness and terminal closure.
    """

    binding_id: str
    session: ResourceRef
    root_turn_id: str
    revisions: tuple[tuple[str, int], ...]
    evidence_id: str
    kind: Literal["terminal_native_inventory", "sealed_request_walk"]

    def __post_init__(self) -> None:
        if (
            not self.evidence_id.strip()
            or not self.root_turn_id.strip()
            or not self.binding_id.strip()
            or not self.revisions
            or type(self.revisions) is not tuple
            or len(self.revisions) > 500
            or len({identity for identity, _ in self.revisions}) != len(self.revisions)
            or any(not identity.strip() or revision < 1 for identity, revision in self.revisions)
            or self.kind not in ("terminal_native_inventory", "sealed_request_walk")
        ):
            raise ValueError("turn usage inventory requires an independent bounded root seal")


@dataclass(frozen=True)
class RevisionRateAttribution:
    """Explicit dated rate and infrastructure evidence for one exact revision.

    Missing model may be bound only from independently admitted model context,
    not from a parent/agent name or merely from the supplied tariff. Inapplicable
    infrastructure explicitly has zero dollars and no duration measurement.
    """

    observation_id: str
    revision: int
    price: ProviderPrice | None = None
    rate_evidence_id: str | None = None
    admitted_model: ModelRef | None = None
    model_evidence_id: str | None = None
    infrastructure_policy: Literal["measured", "not_applicable"] | None = None
    infrastructure_usd: Decimal | None = None
    infrastructure_seconds: Decimal | None = None
    infrastructure_evidence_id: str | None = None

    def __post_init__(self) -> None:
        if not self.observation_id.strip() or self.revision < 1:
            raise ValueError("rates require an exact usage revision")
        for value in (self.infrastructure_usd, self.infrastructure_seconds):
            if value is not None and (not value.is_finite() or value < 0):
                raise ValueError("infrastructure requires nonnegative finite measurements")
        if self.infrastructure_policy not in (None, "measured", "not_applicable"):
            raise ValueError("unknown infrastructure policy")
        if self.infrastructure_policy == "not_applicable" and (
            self.infrastructure_usd != 0 or self.infrastructure_seconds is not None
        ):
            raise ValueError("inapplicable infrastructure requires explicit zero dollars, no time")


@dataclass(frozen=True)
class NormalizedUsageRevision:
    observation: UsageObservation
    latest: bool
    selected: bool
    settled: bool | None
    model: ModelRef | None
    attribution: RevisionRateAttribution | None
    model_usd: Decimal | None
    infrastructure_usd: Decimal | None
    infrastructure_seconds: Decimal | None
    total_usd: Decimal | None
    ledger_delta_usd: Decimal | None
    issues: tuple[str, ...]


@dataclass(frozen=True)
class TurnAccountingSnapshot:
    """Complete only with closure, pricing, infrastructure and settled ledger.

    known_model_usd is a subtotal, never a substitute for an unknown total.
    Token counts and native meters in every revision remain untouched.
    """

    revisions: tuple[NormalizedUsageRevision, ...]
    unattributed: tuple[UsageObservation, ...]
    inventory: TurnUsageInventory | None
    coverage_complete: bool
    pricing_complete: bool
    settlement_complete: bool
    complete: bool
    total_usd: Decimal | None
    expected_ledger_usd: Decimal | None
    ledger_usd: Decimal | None
    known_model_usd: Decimal
    issues: tuple[str, ...]


def _present(value: str | None) -> bool:
    return value is not None and bool(value.strip())


def _price_revision(
    observation: UsageObservation, attribution: RevisionRateAttribution | None
) -> tuple[ModelRef | None, Decimal | None, Decimal | None, tuple[str, ...]]:
    issues: list[str] = []
    model = observation.model
    if attribution is not None and attribution.admitted_model is not None:
        if not _present(attribution.model_evidence_id):
            issues.append("MODEL_EVIDENCE_MISSING")
        elif model is None:
            model = attribution.admitted_model
        elif model != attribution.admitted_model:
            issues.append("MODEL_ATTRIBUTION_MISMATCH")
    if model is None:
        issues.append("MODEL_UNKNOWN")
    price = attribution.price if attribution is not None else None
    if (
        price is None
        or not price.verified
        or not _present(attribution.rate_evidence_id if attribution is not None else None)
    ):
        issues.append("PRICE_UNKNOWN")
    elif (
        model is None
        or (price.provider, price.model)
        != (
            observation.session.provider,
            model.id,
        )
        or model.provider != observation.session.provider
    ):
        issues.append("PRICE_ATTRIBUTION_MISMATCH")
    elif price.checked_on > observation.observed_at.date():
        issues.append("PRICE_POSTDATES_USAGE")
    if observation.completeness != "measured" or not observation.native_meter:
        issues.append("USAGE_INCOMPLETE")
    model_cost: Decimal | None = None
    if not issues and price is not None:
        # Bind admitted model only in this local projection, never in the DTO/DB.
        model_cost = provider_cost_of(
            observation.model_copy(update={"model": model}), price, infrastructure_usd=Decimal(0)
        )
        if model_cost is None:
            issues.append("TOKEN_STAGES_UNKNOWN")
    if (
        attribution is None
        or attribution.infrastructure_policy is None
        or attribution.infrastructure_usd is None
        or not _present(attribution.infrastructure_evidence_id)
    ):
        issues.append("INFRASTRUCTURE_UNKNOWN")
    elif attribution.infrastructure_policy == "measured" and (
        attribution.infrastructure_seconds is None
    ):
        issues.append("CONTAINER_TIME_UNKNOWN")
    total = (
        model_cost + attribution.infrastructure_usd
        if not issues
        and model_cost is not None
        and attribution is not None
        and attribution.infrastructure_usd is not None
        else None
    )
    return model, model_cost, total, tuple(issues)


@dataclass(frozen=True)
class TurnAccountingReader:
    sessions: async_sessionmaker[AsyncSession]
    scope: Scope
    binding: ProviderBinding
    native_session: ResourceRef
    root_turn_id: str

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
            raise ValueError("turn accounting requires one consistent database bind")
        uuid.UUID(self.scope.tenant_id)
        if (
            self.scope.is_platform
            or self.scope.is_legacy_host_authorized
            or self.native_session.kind != "session"
            or self.native_session.tenant_id != self.scope.tenant_id
            or self.native_session.account_id != self.scope.account_id
            or self.binding.provider != self.native_session.provider
            or self.binding.legacy_account_id not in (None, self.scope.account_id)
            or self.binding.thread.channel.tenant_id != self.scope.tenant_id
            or self.binding.native_refs.get("session") != self.native_session.id
            or not self.root_turn_id.strip()
        ):
            raise ValueError("turn accounting requires an authorized tenant binding/session/root")

    async def snapshot(
        self,
        *,
        attributions: tuple[RevisionRateAttribution, ...] = (),
        inventory: TurnUsageInventory | None = None,
        markup: Decimal = Decimal(1),
    ) -> TurnAccountingSnapshot:
        """Read a coherent READ ONLY snapshot; never reprice or settle stored rows."""
        self._admit()  # Repeat before connecting; factories may have been reconfigured.
        if not markup.is_finite() or markup < 0:
            raise ValueError("markup must be finite and nonnegative")
        rates = {(item.observation_id, item.revision): item for item in attributions}
        if len(rates) != len(attributions):
            raise ValueError("duplicate revision rate attribution")
        if inventory is not None and (
            inventory.binding_id != self.binding.id
            or inventory.session != self.native_session
            or inventory.root_turn_id != self.root_turn_id
        ):
            raise ValueError("native inventory belongs to another binding/session/root")
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            rows = await read_accounting_revisions(
                session, tenant_id=uuid.UUID(self.scope.tenant_id), binding=self.binding
            )
        with localcontext() as context:
            context.prec = 80
            return self._normalize(rows, rates, inventory, markup)

    def _normalize(
        self,
        rows: UsageAccountingRows,
        rates: dict[tuple[str, int], RevisionRateAttribution],
        inventory: TurnUsageInventory | None,
        markup: Decimal,
    ) -> TurnAccountingSnapshot:
        issues: list[str] = []
        if rows.binding != self.binding:
            issues.append("BINDING_NOT_CURRENT")
        latest: tuple[UsageObservation, ...] = ()
        for applied in rows.revisions:
            if applied.binding_id != self.binding.id or (
                applied.observation_id,
                applied.revision,
            ) != (applied.observation.id, applied.observation.revision):
                raise ValueError("durable usage identity mismatch")
            if applied.observation.session.id == self.native_session.id and (
                applied.observation.session != self.native_session
            ):
                issues.append("NATIVE_SESSION_SCOPE_MISMATCH")
            if applied.observation.session == self.native_session:
                latest = replace_observation(latest, applied.observation)
        members = dict(inventory.revisions) if inventory is not None else {}
        root = tuple(
            value
            for value in latest
            if value.turn_id == self.root_turn_id or (value.turn_id is None and value.id in members)
        )
        unattributed = tuple(
            value for value in latest if value.turn_id is None and value.id not in members
        )
        if unattributed:
            issues.append("ROOT_ATTRIBUTION_UNKNOWN")
        if inventory is None:
            issues.append("TURN_INVENTORY_UNKNOWN")
        elif members != {value.id: value.revision for value in root}:
            issues.append("TURN_INVENTORY_MISMATCH")
        if not root:
            issues.append("ROOT_USAGE_MISSING")
        selected = disjoint_observations(root)
        root_ids = {value.id for value in root}
        revision_keys = {
            (item.observation_id, item.revision)
            for item in rows.revisions
            if item.observation.session == self.native_session and item.observation_id in root_ids
        }
        if rates.keys() - revision_keys:
            raise ValueError("rate attribution must name a persisted revision of this root")
        if any(identity not in root_ids for value in root for identity in value.covers):
            issues.append("COVERAGE_MEMBER_MISSING")
        # The neutral aggregate pricing/settlement model is still explicitly cut.
        if any(value.covers for value in root):
            issues.append("AGGREGATE_COVERAGE_UNSUPPORTED")
        if any(value.grain == "session" for value in root):
            issues.append("SESSION_TO_ROOT_ATTRIBUTION_UNSUPPORTED")
        coverage_complete = not issues
        outbox = {(item.row.observation_id, item.row.revision): item for item in rows.outbox}
        ledger = {item.idempotency_key: item for item in rows.ledger}
        normalized: list[NormalizedUsageRevision] = []
        selected_ids = {value.id for value in selected}
        for applied in rows.revisions:
            value = applied.observation
            if value.session != self.native_session or value.id not in root_ids:
                continue
            key = (value.id, value.revision)
            pending = outbox.get(key)
            if pending is not None and pending.row.observation != value:
                raise ValueError("outbox and durable usage revision disagree")
            model, model_cost, total, revision_issues = _price_revision(value, rates.get(key))
            rate = rates.get(key)
            natural_key = (
                f"turn:{value.session.id}:{value.id}"
                if value.revision == 1
                else (f"adjust:{self.binding.id}:{value.id}:{value.revision}")
            )
            entry = ledger.get(natural_key)
            if entry is not None and (
                entry.channel_id != self.binding.thread.channel.channel_id
                or entry.reason != "turn_debit"
            ):
                raise ValueError("ledger entry belongs to another billing context")
            normalized.append(
                NormalizedUsageRevision(
                    observation=value,
                    latest=value.revision == next(v.revision for v in root if v.id == value.id),
                    selected=value.id in selected_ids and value in selected,
                    settled=pending.settled if pending is not None else None,
                    model=model,
                    attribution=rate,
                    model_usd=model_cost,
                    infrastructure_usd=rate.infrastructure_usd if rate is not None else None,
                    infrastructure_seconds=rate.infrastructure_seconds
                    if rate is not None
                    else None,
                    total_usd=total,
                    ledger_delta_usd=entry.delta_usd if entry is not None else None,
                    issues=revision_issues,
                )
            )
        current = tuple(item for item in normalized if item.selected)
        pricing_complete = bool(current) and all(item.total_usd is not None for item in current)
        infrastructure_ids = [
            item.attribution.infrastructure_evidence_id
            for item in current
            if item.attribution is not None
            and item.infrastructure_usd is not None
            and item.infrastructure_usd > 0
        ]
        if len(set(infrastructure_ids)) != len(infrastructure_ids):
            issues.append("INFRASTRUCTURE_COVERAGE_OVERLAP")
            pricing_complete = False
        if not pricing_complete:
            issues.append("PRICING_INCOMPLETE")
        settlement_complete = bool(normalized) and all(item.settled is True for item in normalized)
        if not settlement_complete:
            issues.append("OUTBOX_NOT_SETTLED")
        known_model = sum(
            (item.model_usd for item in current if item.model_usd is not None), Decimal(0)
        )
        total = (
            sum((item.total_usd for item in current if item.total_usd is not None), Decimal(0))
            if (coverage_complete and pricing_complete)
            else None
        )
        expected: Decimal | None = None
        if total is not None:
            amounts: list[Decimal] = []
            for item in current:
                rate = item.attribution
                if rate is None or rate.price is None or item.total_usd is None:
                    raise ValueError("complete pricing requires rate attribution")
                if item.observation.session.provider == "anthropic":
                    if rate.infrastructure_usd != 0:
                        issues.append("ANTHROPIC_INFRASTRUCTURE_UNSUPPORTED")
                        break
                    price = rate.price
                    legacy = cost_of(
                        item.observation,
                        ModelRates(
                            float(price.input),
                            float(price.output),
                            float(price.cache_write or 0),
                            float(price.cache_read),
                        ),
                    )
                    if legacy is None:
                        issues.append("ANTHROPIC_TOKEN_STAGES_UNKNOWN")
                        break
                    amounts.append(debit_amount(legacy, markup=markup))
                else:
                    amounts.append((item.total_usd * markup).quantize(Decimal("0.000001")))
            else:
                expected = sum(amounts, Decimal(0))
        deltas = [item.ledger_delta_usd for item in normalized if item.ledger_delta_usd is not None]
        ledger_total = -sum(deltas, Decimal(0)) if deltas else None
        if ledger_total is None:
            issues.append("LEDGER_AMOUNT_UNKNOWN")
        elif expected is not None and ledger_total != expected:
            issues.append("LEDGER_COST_MISMATCH")
        return TurnAccountingSnapshot(
            tuple(normalized),
            unattributed,
            inventory,
            coverage_complete,
            pricing_complete,
            settlement_complete,
            not issues,
            total,
            expected,
            ledger_total,
            known_model,
            tuple(issues),
        )
