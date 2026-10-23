"""Delegated usage attribution without inventing ancestry, meters or debits.

Native subagent settlement requires authoritative same-session ancestry. Host
workers retain independent native sessions and original accounting identities;
their rollup is a read only. Neither path enables delegation or collects native
records.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext

from daimon.core.accounting_outbox import record_observation_usage
from daimon.core.pricing import ModelRates, ProviderPrice, minimum_cost_of, provider_cost_of
from daimon.core.stores.accounting_outbox import settled_amount
from daimon.core.stores.root_usage import lock_root_usage
from daimon.core.stores.turn_usage import UsageAccountingRows, read_accounting_revisions
from daimon.core.usage_aggregation import disjoint_observations, replace_observation
from daimon.core.usage_recording import TurnLedgerReason
from mux.contracts.ids import ModelRef, ResourceRef, Scope
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.errors import ScopeViolation
from mux.profiles import accounting_mode_for
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def root_usage_outcome_id(
    scope: Scope, session: ResourceRef, *, previous: uuid.UUID | None, fresh: bool
) -> uuid.UUID | None:
    """Retain the first observer at its input claim, including an unknown join.

    Caller owns the atomic counted root operation. Replays keep the original
    id or missing marker, never a fresh observer. This function writes nothing.
    """
    if scope.is_platform or scope.is_legacy_host_authorized or session.kind != "session":
        raise ValueError("root usage requires an admitted tenant session")
    if (session.tenant_id, session.account_id) != (scope.tenant_id, scope.account_id):
        raise ValueError("root usage session differs from admission")
    if not fresh:
        return previous
    if previous is not None:
        raise ValueError("fresh input already has an outcome association")
    from daimon.core.turn.outcomes import current_outcome

    outcome = current_outcome.get()
    if outcome is None:
        return None
    if (str(outcome.tenant_id), str(outcome.account_id), outcome.session_id) != (
        scope.tenant_id,
        scope.account_id,
        session.id,
    ):
        raise ValueError("root usage observer differs from admission")
    return outcome.id


@dataclass(frozen=True)
class HostUsageBinding:
    """Host-authorized worker session; native subagent ancestry is independent."""

    binding: ProviderBinding
    session: ResourceRef


@dataclass(frozen=True)
class DelegatedUsageRead:
    """An attribution read, never a second root debit or proof of final actual.

    token_floor_usd is a provider list-price floor, excluding markup and
    unknown prices/runtime. ledger_debit_usd is the original customer debit,
    including markup and signed corrections. These amounts overlap and must
    not be added. Pending revisions and zero floors do not prove final actual.
    """

    observations: tuple[UsageObservation, ...]
    token_floor_usd: Decimal
    ledger_debit_usd: Decimal
    unpriced_observations: int
    pending_revisions: int


async def read_delegated_usage(
    session: AsyncSession,
    *,
    scope: Scope,
    bindings: tuple[HostUsageBinding, ...],
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    rows: Mapping[str, UsageAccountingRows] | None = None,
) -> DelegatedUsageRead:
    """Read distinct host workers through their existing usage/outbox bindings.

    The caller loads bindings from its authorized durable worker graph and owns
    a coherent read transaction. All worker turns are included, even when a
    turn ID is missing. Original provider/session/observation IDs stay intact.
    Revisions replace earlier snapshots; native coverage selects disjoint work.
    This port performs no claims, debits, settlement, or reservation changes.
    `rows` are this transaction's `read_accounting_revisions` of some bindings, by
    binding id; they are used instead of reading those bindings again.
    """
    if scope.is_platform or scope.is_legacy_host_authorized or not bindings:
        raise ValueError("delegated usage requires admitted tenant worker bindings")
    tenant_id = uuid.UUID(scope.tenant_id)
    binding_ids: set[str] = set()
    sessions: set[ResourceRef] = set()
    for member in bindings:
        binding, native = member.binding, member.session
        if (
            native.kind != "session"
            or native.tenant_id != scope.tenant_id
            or native.account_id != scope.account_id
            or binding.thread.channel.tenant_id != scope.tenant_id
            or binding.legacy_account_id not in (None, scope.account_id)
            or binding.provider != native.provider
            or binding.native_refs.get("session") != native.id
        ):
            raise ScopeViolation(binding.id, "worker usage differs from its admitted session")
        if binding.id in binding_ids or native in sessions:
            raise ValueError("delegated usage cannot count a worker binding/session twice")
        binding_ids.add(binding.id)
        sessions.add(native)

    observations: list[UsageObservation] = []
    keys: set[str] = set()
    floor, debits = Decimal(0), Decimal(0)
    unpriced, pending = 0, 0
    with localcontext() as arithmetic:
        arithmetic.prec = 80
        for member in bindings:
            read = (
                rows[member.binding.id]
                if rows is not None and member.binding.id in rows
                else await read_accounting_revisions(
                    session, tenant_id=tenant_id, binding=member.binding
                )
            )
            if read.binding != member.binding:
                raise ValueError("worker binding changed during usage attribution")
            latest: tuple[UsageObservation, ...] = ()
            for revision in read.revisions:
                value = revision.observation
                if value.session != member.session or (
                    value.model is not None and value.model.provider != member.session.provider
                ):
                    raise ScopeViolation(value.id, "worker usage contains another session")
                latest = replace_observation(latest, value)
            for value in disjoint_observations(latest):
                observations.append(value)
                amount = minimum_cost_of(
                    value,
                    prices.get((value.model.provider, value.model.id)) if value.model else None,
                )
                if amount is None:
                    unpriced += 1
                else:
                    floor += amount
            pending += sum(not item.settled for item in read.outbox)
            for row in read.ledger:
                if row.idempotency_key in keys:
                    raise ValueError("delegated usage has overlapping monetary identities")
                keys.add(row.idempotency_key)
                debits -= row.delta_usd
    return DelegatedUsageRead(tuple(observations), floor, debits, unpriced, pending)


@dataclass(frozen=True)
class RootUsageMember:
    """Host-proven native ancestry link; no provider parent field is assumed.

    None model means unaudited pricing, even if a parent has a known tariff.
    """

    native_turn_id: str
    parent_turn_id: str | None
    thread_id: str | None
    model: ModelRef | None = None

    @property
    def observation_id(self) -> str:
        return f"openai:turn:{self.native_turn_id}"


@dataclass(frozen=True)
class RootUsageScope:
    """Admitted session and complete, caller-proven native root/descendant tree.

    A reused subagent does not prove membership of its old turns. The host must
    supply the parent of each distinct native turn, including nested delegation.
    Parent links must resolve to this root. Children do not inherit its model.
    """

    session: ResourceRef
    root_turn_id: str
    members: tuple[RootUsageMember, ...]
    disjoint: bool = False
    inventory_complete: bool = False

    def __post_init__(self) -> None:
        if type(self.members) is not tuple or not 0 < len(self.members) <= 500:
            raise ValueError("root usage requires a frozen bounded native inventory")
        if type(self.disjoint) is not bool or type(self.inventory_complete) is not bool:
            raise ValueError("coverage proofs must be explicit verified host decisions")
        if (
            self.session.kind != "session"
            or not accounting_mode_for(self.session.provider).supports_root_usage
        ):
            raise ValueError("root usage requires an OpenAI native session")
        if not self.root_turn_id or not self.session.tenant_id:
            raise ValueError("root usage requires an admitted tenant and native root")
        members = {member.native_turn_id: member for member in self.members}
        if len(members) != len(self.members) or self.root_turn_id not in members:
            raise ValueError("root usage requires unique native turns and the root")
        for member in self.members:
            if not member.native_turn_id or (
                member.model is not None and member.model.provider != self.session.provider
            ):
                raise ValueError("root usage member has invalid native/model identity")
            if member.native_turn_id == self.root_turn_id:
                if member.parent_turn_id is not None or member.thread_id is not None:
                    raise ValueError("native root cannot have a parent or subagent")
                continue
            if not member.thread_id:
                raise ValueError("delegated native turn requires a subagent identity")
            visited: set[str] = set()
            current = member.native_turn_id
            while current != self.root_turn_id:
                if current in visited:
                    raise ValueError("delegated native ancestry is cyclic")
                visited.add(current)
                parent = members[current].parent_turn_id
                if parent is None or parent not in members:
                    raise ValueError("delegated native ancestry does not reach this root")
                current = parent


@dataclass(frozen=True)
class RootUsageMeasurement:
    observation: UsageObservation
    provider_price: ProviderPrice | None = None


def prepare_root_usage(
    scope: RootUsageScope, measurements: tuple[RootUsageMeasurement, ...]
) -> tuple[RootUsageMeasurement, ...]:
    """Preserve native IDs/meters, projecting only authorized root/model context.

    OpenAI root and child turn usage are separate model work. No synthetic
    aggregate, covers mutation, session total or parent-model inference occurs.
    Native child turn ID remains in its canonical observation ID; accounting
    turn_id is the root, durably immutable across revisions and restarts.
    """
    members = {member.observation_id: member for member in scope.members}
    if len(measurements) != len(members) or {m.observation.id for m in measurements} != set(
        members
    ):
        raise ValueError("root usage batch requires every inventoried observation exactly once")
    prepared: list[RootUsageMeasurement] = []
    for measurement in measurements:
        value = measurement.observation
        member = members[value.id]
        if value.session != scope.session:
            raise ScopeViolation(value.id, "root usage belongs to another authorized session")
        if (
            value.turn_id != member.native_turn_id
            or value.thread_id != member.thread_id
            or value.grain != "turn"
            or value.basis != "cumulative"
            or value.covers
        ):
            raise ValueError("root usage requires disjoint native turn measurements")
        if member.model is not None and value.model not in (None, member.model):
            raise ScopeViolation(value.id, "usage differs from the authorized native turn model")
        if (
            value.input_tokens is not None
            and value.input_cached_tokens is not None
            and value.input_cached_tokens + (value.input_cache_write_tokens or 0)
            > value.input_tokens
        ):
            raise ValueError("cached input exceeds inclusive input tokens")
        price = (
            measurement.provider_price
            if member.model is not None and scope.disjoint and scope.inventory_complete
            else None
        )
        canonical = value.model_copy(
            update={"turn_id": scope.root_turn_id, "model": member.model or value.model}
        )
        if price is not None:
            # Validate every supplied pricing identity/count before any writes.
            provider_cost_of(canonical, price, infrastructure_usd=None)
        prepared.append(RootUsageMeasurement(canonical, price))
    return tuple(sorted(prepared, key=lambda item: item.observation.id))


async def record_root_usage(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    binding_id: str,
    scope: RootUsageScope,
    measurements: tuple[RootUsageMeasurement, ...],
    tenant_id: uuid.UUID,
    platform_user_id: str | None,
    infrastructure_usd: Decimal | None,
    markup: Decimal = Decimal("1"),
    reason: TurnLedgerReason = "turn_debit",
    channel_id: str | None = None,
) -> tuple[bool, ...]:
    """Atomically persist/settle all root and child observations, then capture.

    infrastructure_usd is the independently measured TOTAL for this root and
    its descendants' shared session/environment and applicable auxiliary costs.
    It is charged once on the root; children add their own model work only.
    None leaves every unsatisfied measurement durably pending. Unverified
    inventory completeness or disjointness also keeps every new charge pending.
    Zero is an explicit audited total, never a default or an inferred native
    meter field.
    New measured amounts after settlement require a higher root revision.
    Returns per-observation claim booleans in canonical-ID order; False may be
    replay or pending, as with record_provider_usage. Caller owns admission.
    """
    from daimon.core.turn.outcomes import current_outcome

    if scope.session.tenant_id != str(tenant_id):
        raise ScopeViolation(scope.session.id, "root usage belongs to another tenant")
    if infrastructure_usd is not None and (
        not infrastructure_usd.is_finite() or infrastructure_usd < 0
    ):
        raise ValueError("root infrastructure requires measured nonnegative actual dollars")
    prepared = prepare_root_usage(scope, measurements)
    outcome = current_outcome.get()
    if outcome is not None:
        outcome.check_root_usage(scope.root_turn_id, tuple(m.observation for m in prepared))

    def infrastructure(value: UsageObservation) -> Decimal | None:
        if infrastructure_usd is None:
            return None
        return infrastructure_usd if value.thread_id is None else Decimal(0)

    applied: list[bool] = []
    async with sessionmaker() as session, session.begin():
        settled = await lock_root_usage(
            session,
            binding_id,
            tuple(m.observation for m in prepared),
            tenant_id=tenant_id,
        )
        by_id = {m.observation.id: m for m in prepared}
        for row in settled:
            measurement = by_id[row.observation_id]
            if (
                measurement.observation.revision != row.revision
                or measurement.provider_price is None
            ):
                continue
            cost = provider_cost_of(
                measurement.observation,
                measurement.provider_price,
                infrastructure_usd=infrastructure(measurement.observation),
            )
            if cost is not None:
                _, previous = await settled_amount(
                    session, row, tenant_id=tenant_id, channel_id=channel_id
                )
                with localcontext() as context:
                    context.prec = 80
                    amount = (cost * markup).quantize(Decimal("0.000001"))
                if amount != previous:
                    raise ValueError("changed settled root usage cost requires a new revision")
        for measurement in prepared:
            value = measurement.observation
            applied.append(
                await record_observation_usage(
                    session,
                    binding_id=binding_id,
                    observation=value,
                    tenant_id=tenant_id,
                    platform_user_id=platform_user_id,
                    pricing=None,
                    provider_price=measurement.provider_price,
                    infrastructure_usd=infrastructure(value),
                    billing_grain="turn",
                    markup=markup,
                    reason=reason,
                    channel_id=channel_id,
                )
            )
    if outcome is not None:
        for measurement in prepared:
            outcome.note_usage(
                measurement.observation,
                metered=True,
                provider_price=measurement.provider_price,
                infrastructure_usd=infrastructure(measurement.observation),
                reuse_provider_price=False,
            )
    return tuple(applied)
