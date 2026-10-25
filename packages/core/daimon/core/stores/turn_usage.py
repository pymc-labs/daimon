"""Read-only per-turn telemetry queries. Billing remains authoritative elsewhere."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID

from daimon.core._models import (
    AccountingOutbox,
    TenantLedger,
    TurnOutcome,
    UsageEvent,
    UsageObservation,
)
from daimon.core.context_prompt import TurnContext
from daimon.core.fleet import FleetRun, worker_slot
from daimon.core.stores import check_persisted_read, fleet, mux_state, read_persisted_columns
from daimon.core.stores.domain import TenantLedgerRow, UsageEventRow
from mux.contracts.resources import ProviderBinding
from mux.profiles import MANAGED_AGENTS
from mux.state.lease import Slot
from mux.state.store import SESSION_REF, binding_slot
from mux.state.usage_ledger import AppliedUsage, OutboxRow
from pydantic import BaseModel, ConfigDict
from sqlalchemy import RowMapping, Select, case, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement


@dataclass(frozen=True)
class AccountingRevision:
    row: OutboxRow
    settled: bool


@dataclass(frozen=True)
class UsageAccountingRows:
    """Persisted facts only: no pricing, root closure or settlement inferred."""

    binding: ProviderBinding | None
    revisions: tuple[AppliedUsage, ...]
    outbox: tuple[AccountingRevision, ...]
    ledger: tuple[TenantLedgerRow, ...]
    usage_events: tuple[UsageEventRow, ...] = ()
    usage_refs: tuple[tuple[str, str], ...] = ()
    attribution_complete: bool | None = None


@dataclass(frozen=True)
class FleetWorkerAccountingRows:
    worker_id: UUID
    worker_version: int
    reserved_usd: Decimal
    binding_id: str | None
    rows: UsageAccountingRows | None
    issue: Literal["binding_not_accepted", "binding_unavailable"] | None


@dataclass(frozen=True)
class FleetAccountingRows:
    """Original product exposure and billing facts, never provider actuals.

    A private worker binding attributes its persisted revisions to this run.
    Root sessions can span runs and are deliberately excluded. Missing bindings
    remain gaps; no row absence proves free work or releases a reservation.
    Reserved amounts are total exposure including recorded debits, not separate
    cash holds to add to those debits. External cash receipt joins are independent.
    """

    run: FleetRun
    workers: tuple[FleetWorkerAccountingRows, ...]


@dataclass(frozen=True)
class AccountingPrefetch:
    """The accounting rows of many worker slots, read once in the caller's transaction.

    Keyed by what each per-binding read of `read_accounting_revisions` filters on, so
    that read can answer from here; anything not read here is read as before.
    """

    tenant_id: UUID
    bindings: mux_state.SlotBindings
    revisions: Mapping[str, tuple[tuple[str, int, Any], ...]]
    outbox: Mapping[str, tuple[tuple[str, int, Any, datetime | None], ...]]
    ledger_keys: frozenset[str]
    ledger: Mapping[str, tuple[RowMapping, ...]]
    #: A Managed Agents binding's native session -> its usage_events ids.
    session_events: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: dict[str, tuple[str, ...]]()
    )


def _columns(statement: Select[Any]) -> Select[Any]:
    # The projection `read_persisted_columns` reads: columns, never cached ORM entities.
    return statement.with_only_columns(*statement.selected_columns)


async def prefetch_accounting(
    session: AsyncSession, *, tenant_id: UUID, slots: Iterable[Slot]
) -> AccountingPrefetch:
    """Read the bindings, revisions, outbox and ledger rows of `slots` in five statements.

    Reads only, refusing nothing: each per-binding read answered from this applies its
    own checks and refusals when it runs. Caller owns the transaction and its snapshot.
    """
    with session.no_autoflush:
        bindings = await mux_state.read_slot_bindings(session, slots)
        ids = sorted({pair[0] for pair in bindings.slots.values() if pair is not None})
        revisions: dict[str, list[tuple[str, int, Any]]] = {identity: [] for identity in ids}
        outbox: dict[str, list[tuple[str, int, Any, datetime | None]]] = {
            identity: [] for identity in ids
        }
        keys: set[str] = set()
        if ids:
            for row in await session.execute(
                select(
                    UsageObservation.binding_id,
                    UsageObservation.observation_id,
                    UsageObservation.revision,
                    UsageObservation.applied,
                )
                .where(
                    UsageObservation.tenant_id == tenant_id, UsageObservation.binding_id.in_(ids)
                )
                .order_by(
                    UsageObservation.binding_id,
                    UsageObservation.observation_id,
                    UsageObservation.revision,
                )
            ):
                revisions[row.binding_id].append((row.observation_id, row.revision, row.applied))
            for row in await session.execute(
                select(
                    AccountingOutbox.binding_id,
                    AccountingOutbox.observation_id,
                    AccountingOutbox.revision,
                    AccountingOutbox.row,
                    AccountingOutbox.applied_at,
                )
                .where(
                    AccountingOutbox.tenant_id == tenant_id, AccountingOutbox.binding_id.in_(ids)
                )
                .order_by(
                    AccountingOutbox.binding_id,
                    AccountingOutbox.observation_id,
                    AccountingOutbox.revision,
                )
            ):
                outbox[row.binding_id].append(
                    (row.observation_id, row.revision, row.row, row.applied_at)
                )
                key = _ledger_key(row.binding_id, row.observation_id, row.revision, row.row)
                if key is not None:
                    keys.add(key)
        managed = sorted(
            {
                native
                for body in bindings.bodies.values()
                if body is not None and (native := _managed_session_body(body)) is not None
            }
        )
        events: dict[str, list[str]] = {native: [] for native in managed}
        if managed:
            for row in await session.execute(_session_events_query(tenant_id, managed)):
                events[row.managed_session_id].append(row.event_id)
                keys.add(session_ledger_key(row.managed_session_id, row.event_id))
        ledger: dict[str, list[RowMapping]] = {key: [] for key in keys}
        if keys:
            for row in (
                await session.execute(
                    _columns(
                        select(TenantLedger).where(
                            TenantLedger.tenant_id == tenant_id,
                            TenantLedger.idempotency_key.in_(keys),
                        )
                    )
                )
            ).mappings():
                ledger[row["idempotency_key"]].append(row)
    return AccountingPrefetch(
        tenant_id,
        bindings,
        {identity: tuple(rows) for identity, rows in revisions.items()},
        {identity: tuple(rows) for identity, rows in outbox.items()},
        frozenset(keys),
        {key: tuple(rows) for key, rows in ledger.items()},
        {native: tuple(ids) for native, ids in events.items()},
    )


def _managed_session_body(body: Mapping[str, Any]) -> str | None:
    """A stored binding body's Managed Agents session, as `managed_session` reads it."""
    if body.get("profile") != MANAGED_AGENTS.profile_id:
        return None
    refs = body.get("native_refs")
    native = cast(Mapping[str, object], refs).get(SESSION_REF) if isinstance(refs, dict) else None
    return native if isinstance(native, str) else None


def session_ledger_key(managed_session_id: str, event_id: str) -> str:
    """The debit key `record_turn_usage` writes for one Managed Agents model call."""
    return f"turn:{managed_session_id}:{event_id}"


def managed_session(binding: ProviderBinding) -> str | None:
    """A Managed Agents binding's native session; None for any other profile."""
    if binding.profile != MANAGED_AGENTS.profile_id:
        return None
    return binding.native_refs.get(SESSION_REF)


def _session_events_query(tenant_id: UUID, sessions: Iterable[str]) -> Select[Any]:
    return (
        select(UsageEvent.managed_session_id, UsageEvent.event_id)
        .where(UsageEvent.tenant_id == tenant_id, UsageEvent.managed_session_id.in_(sessions))
        .order_by(UsageEvent.managed_session_id, UsageEvent.event_id)
    )


@dataclass(frozen=True)
class SessionDebits:
    """One Managed Agents session's recorded model calls and their ledger debits."""

    events: int
    debited: int
    debit_usd: Decimal


async def read_session_debits(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    managed_session_id: str,
    prefetch: AccountingPrefetch | None = None,
) -> SessionDebits:
    """What a Managed Agents session has been debited: its usage_events rows and the
    ledger rows keyed exactly `turn:<session>:<event_id>` for them, nothing else.

    MA usage has no outbox: the live recorder and the usage sweep write a call's
    usage_events row and its debit in one transaction, keyed by session and event.
    Reads only. A `prefetch` from this transaction answers it with the same checks.
    """
    query = _session_events_query(tenant_id, (managed_session_id,))
    if prefetch is not None and managed_session_id in prefetch.session_events:
        check_persisted_read(session, query)
        ids: Iterable[str] = prefetch.session_events[managed_session_id]
    else:
        ids = [row.event_id for row in await read_persisted_columns(session, query)]
    keys = {session_ledger_key(managed_session_id, event_id) for event_id in ids}
    ledger_query = select(TenantLedger.idempotency_key, TenantLedger.delta_usd).where(
        TenantLedger.tenant_id == tenant_id, TenantLedger.idempotency_key.in_(keys)
    )
    if prefetch is not None and keys <= prefetch.ledger_keys:
        check_persisted_read(session, ledger_query)
        rows = [(key, row["delta_usd"]) for key in keys for row in prefetch.ledger[key]]
    else:
        rows = (
            [tuple(row) for row in await read_persisted_columns(session, ledger_query)]
            if keys
            else []
        )
    return SessionDebits(
        events=len(keys),
        debited=len({key for key, _ in rows}),
        debit_usd=-sum((delta for _, delta in rows), Decimal(0)),
    )


def _ledger_key(binding_id: str, observation_id: str, revision: int, body: object) -> str | None:
    """The ledger key an outbox row's debit carries, from its stored columns and JSON.

    A guess for the batched read only: the per-binding read derives each key from the
    validated row again, and reads any key not fetched here itself.
    """
    if revision != 1:
        return f"adjust:{binding_id}:{observation_id}:{revision}"
    session_id: object = body
    for name in ("observation", "session", "id"):
        session_id = (
            cast(dict[str, object], session_id).get(name) if isinstance(session_id, dict) else None
        )
    return f"turn:{session_id}:{observation_id}" if isinstance(session_id, str) else None


async def read_fleet_accounting_revisions(
    session: AsyncSession, *, tenant_id: UUID, run_id: UUID
) -> FleetAccountingRows:
    """Read one tenant's exact run through its durable private worker slots.

    Caller owns authorization and a coherent read-only transaction. No flush,
    claims, locks for update, exposure refresh, repricing or monetary writes.
    Returned revision bodies are owner facts, not a public export schema.
    """
    with session.no_autoflush:
        run, workers = await fleet.run_status(
            session, tenant_id=tenant_id, run_id=run_id, for_update=False
        )
        result: list[FleetWorkerAccountingRows] = []
        for worker in workers:
            slot = worker_slot(run, worker)
            binding = await mux_state.get_binding(session, slot, read_persisted=True)
            issue = None if worker.binding_id is not None else "binding_not_accepted"
            rows = None
            if binding is None:
                issue = "binding_unavailable" if worker.binding_id is not None else issue
            else:
                if (
                    binding_slot(binding) != slot
                    or binding.provider != worker.backend
                    or binding.profile != worker.profile
                    or worker.binding_id not in (None, binding.id)
                    or not binding.native_refs.get("session")
                ):
                    raise ValueError("fleet accounting binding differs from its worker")
                rows = await read_accounting_revisions(
                    session, tenant_id=tenant_id, binding=binding
                )
                if rows.binding != binding:
                    raise ValueError("fleet accounting binding changed during capture")
            result.append(
                FleetWorkerAccountingRows(
                    worker.id, worker.version, worker.reserved_usd, worker.binding_id, rows, issue
                )
            )
        return FleetAccountingRows(run, tuple(result))


async def _outcome_usage_refs(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    account_id: UUID,
    binding: ProviderBinding,
    outcome_ids: tuple[UUID | None, ...],
) -> tuple[frozenset[tuple[str, str]], bool]:
    native = binding.native_refs.get("session")
    if not native or binding.legacy_account_id not in (None, str(account_id)):
        raise ValueError("root outcome admission differs from its binding")
    requested = {identity for identity in outcome_ids if identity is not None}
    rows = await read_persisted_columns(
        session,
        select(TurnOutcome.id, TurnOutcome.usage_refs).where(
            TurnOutcome.tenant_id == tenant_id,
            TurnOutcome.account_id == account_id,
            TurnOutcome.session_id == native,
            TurnOutcome.id.in_(requested),
        ),
    )
    found: set[UUID] = set()
    refs: set[tuple[str, str]] = set()
    complete = bool(outcome_ids) and None not in outcome_ids
    for identity, values in rows:
        found.add(identity)
        complete = complete and bool(values)
        for value in values:
            if set(value) != {"session_id", "event_id"} or any(
                not isinstance(part, str) or not part for part in value.values()
            ):
                raise ValueError("root outcome usage reference is malformed")
            if value["session_id"] != native:
                raise ValueError("root outcome usage reference differs from its session")
            refs.add((value["session_id"], value["event_id"]))
    return frozenset(refs), complete and found == requested


async def read_accounting_revisions(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    binding: ProviderBinding,
    outcome_ids: tuple[UUID | None, ...] | None = None,
    account_id: UUID | None = None,
    prefetch: AccountingPrefetch | None = None,
) -> UsageAccountingRows:
    """Read all binding revisions without applier locks, claims or writes.

    Caller owns transaction consistency and authorization. Without outcome_ids,
    include all revisions. Root selectors come only from the original counted
    operation's immutable observer association, with its admitted account.
    attribution_complete certifies only that join, never final spend/coverage,
    price or infrastructure; unknown association stays pending. No writer,
    synthetic observation or session-wide attribution is introduced.
    A `prefetch` from this transaction answers the reads it covers (all revisions
    only), with the same checks; it never changes the result.
    """
    if binding.thread.channel.tenant_id != str(tenant_id):
        raise ValueError("accounting binding must belong to the requested tenant")
    # SELECTs must not implicitly flush pending objects in the caller's session.
    with session.no_autoflush:
        refs = None
        complete = None
        if outcome_ids is not None:
            if account_id is None:
                raise ValueError("root outcome attribution requires its admitted account")
            refs, complete = await _outcome_usage_refs(
                session,
                tenant_id=tenant_id,
                account_id=account_id,
                binding=binding,
                outcome_ids=outcome_ids,
            )
        rows = await _read_accounting_revisions(
            session, tenant_id=tenant_id, binding=binding, usage_refs=refs, prefetch=prefetch
        )
        if refs is not None:
            found = {(item.managed_session_id, item.event_id) for item in rows.usage_events}
            found.update(
                (item.observation.session.id, item.observation_id) for item in rows.revisions
            )
            rows = replace(
                rows,
                usage_refs=tuple(sorted(refs)),
                attribution_complete=bool(complete) and refs <= found,
            )
        return rows


async def _read_accounting_revisions(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    binding: ProviderBinding,
    usage_refs: frozenset[tuple[str, str]] | None = None,
    prefetch: AccountingPrefetch | None = None,
) -> UsageAccountingRows:
    cached = (
        prefetch
        if prefetch is not None and usage_refs is None and prefetch.tenant_id == tenant_id
        else None
    )
    revision_query = (
        select(UsageObservation.observation_id, UsageObservation.revision, UsageObservation.applied)
        .where(UsageObservation.tenant_id == tenant_id, UsageObservation.binding_id == binding.id)
        .where(
            *(
                ()
                if usage_refs is None
                else (UsageObservation.observation_id.in_({event for _, event in usage_refs}),)
            )
        )
        .order_by(UsageObservation.observation_id, UsageObservation.revision)
    )
    if cached is not None and binding.id in cached.revisions:
        check_persisted_read(session, revision_query)
        revision_rows: Iterable[Any] = cached.revisions[binding.id]
    else:
        revision_rows = await read_persisted_columns(session, revision_query)
    revisions: list[AppliedUsage] = []
    for identity, revision, body in revision_rows:
        value = AppliedUsage.model_validate(body)
        if (value.binding_id, value.observation_id, value.revision) != (
            binding.id,
            identity,
            revision,
        ) or (value.observation.id, value.observation.revision) != (identity, revision):
            raise ValueError("durable usage row identity mismatch")
        if (
            usage_refs is not None
            and (value.observation.session.id, value.observation_id) not in usage_refs
        ):
            raise ValueError("root usage revision differs from its retained reference")
        revisions.append(value)
    current = (
        await mux_state.get_binding(session, binding_slot(binding), read_persisted=True)
        if cached is None
        else await mux_state.cached_binding(
            session, cached.bindings, binding_slot(binding), read_persisted=True
        )
    )
    outbox_query = (
        select(
            AccountingOutbox.observation_id,
            AccountingOutbox.revision,
            AccountingOutbox.row,
            AccountingOutbox.applied_at,
        )
        .where(AccountingOutbox.tenant_id == tenant_id, AccountingOutbox.binding_id == binding.id)
        .where(
            *(
                ()
                if usage_refs is None
                else (AccountingOutbox.observation_id.in_({event for _, event in usage_refs}),)
            )
        )
        .order_by(AccountingOutbox.observation_id, AccountingOutbox.revision)
    )
    if cached is not None and binding.id in cached.outbox:
        check_persisted_read(session, outbox_query)
        rows: Iterable[Any] = cached.outbox[binding.id]
    else:
        rows = await read_persisted_columns(session, outbox_query)
    outbox: list[AccountingRevision] = []
    for identity, revision, body, applied_at in rows:
        row = OutboxRow.model_validate(body)
        if row.key != (binding.id, identity, revision) or (
            row.observation.id,
            row.observation.revision,
        ) != (identity, revision):
            raise ValueError("durable outbox row identity mismatch")
        if (
            usage_refs is not None
            and (row.observation.session.id, row.observation_id) not in usage_refs
        ):
            raise ValueError("root outbox differs from its retained reference")
        outbox.append(AccountingRevision(row, applied_at is not None))
    keys = {
        f"turn:{item.row.observation.session.id}:{item.row.observation_id}"
        if item.row.revision == 1
        else f"adjust:{binding.id}:{item.row.observation_id}:{item.row.revision}"
        for item in outbox
    }
    legacy = (
        tuple(
            UsageEventRow.model_validate(dict(row))
            for row in (
                await read_persisted_columns(
                    session,
                    select(UsageEvent).where(
                        UsageEvent.tenant_id == tenant_id,
                        tuple_(UsageEvent.managed_session_id, UsageEvent.event_id).in_(usage_refs),
                    ),
                )
            ).mappings()
        )
        if usage_refs
        else ()
    )
    keys.update(f"turn:{row.managed_session_id}:{row.event_id}" for row in legacy)
    ledger: tuple[TenantLedgerRow, ...] = ()
    if keys:
        ledger_query = select(TenantLedger).where(
            TenantLedger.tenant_id == tenant_id, TenantLedger.idempotency_key.in_(keys)
        )
        if cached is not None and keys <= cached.ledger_keys:
            check_persisted_read(session, ledger_query)
            ledger_rows: Iterable[RowMapping] = [row for key in keys for row in cached.ledger[key]]
        else:
            ledger_rows = (await read_persisted_columns(session, ledger_query)).mappings()
        ledger = tuple(TenantLedgerRow.model_validate(dict(row)) for row in ledger_rows)
    return UsageAccountingRows(current, tuple(revisions), tuple(outbox), ledger, legacy)


class TurnUsageRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    tenant_id: UUID | None
    account_id: UUID | None
    platform: str
    channel_id: str | None
    thread_id: str | None
    origin: TurnContext
    agent_id: str | None
    session_id: str | None
    reason: str
    started_at: datetime
    duration_ms: int
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None
    model_calls: int | None
    model_ids: list[str] | None
    cost_usd: Decimal | None
    unpriced_calls: int | None
    billing_posture: str | None


class ChannelUsageRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    platform: str
    channel_id: str | None
    origin: TurnContext
    turns: int
    measured_turns: int
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    model_calls: int
    cost_usd: Decimal | None
    known_cost_usd: Decimal
    unpriced_calls: int


def _filters(
    tenant_id: UUID, since: datetime, channel_id: str | None, origin: TurnContext | None
) -> list[ColumnElement[bool]]:
    filters: list[ColumnElement[bool]] = [
        TurnOutcome.tenant_id == tenant_id,
        TurnOutcome.started_at >= since,
    ]
    if channel_id is not None:
        filters.append(TurnOutcome.channel_id == channel_id)
    if origin is not None:
        filters.append(TurnOutcome.origin == origin)
    return filters


async def list_turn_usage(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    since: datetime,
    channel_id: str | None = None,
    origin: TurnContext | None = None,
    limit: int = 100,
) -> list[TurnUsageRow]:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    stmt = (
        select(TurnOutcome.__table__)
        .where(*_filters(tenant_id, since, channel_id, origin))
        .order_by(TurnOutcome.started_at.desc(), TurnOutcome.id)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).mappings()
    return [TurnUsageRow.model_validate(dict(row)) for row in rows]


async def usage_by_channel(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    since: datetime,
    channel_id: str | None = None,
    origin: TurnContext | None = None,
) -> list[ChannelUsageRow]:
    known_cost = func.coalesce(func.sum(TurnOutcome.cost_usd), 0)
    unknown_cost = func.count().filter(TurnOutcome.cost_usd.is_(None))
    stmt = (
        select(
            TurnOutcome.platform,
            TurnOutcome.channel_id,
            TurnOutcome.origin,
            func.count().label("turns"),
            func.count(TurnOutcome.model_calls).label("measured_turns"),
            func.coalesce(func.sum(TurnOutcome.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(TurnOutcome.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(TurnOutcome.cache_read_input_tokens), 0).label(
                "cache_read_input_tokens"
            ),
            func.coalesce(func.sum(TurnOutcome.cache_creation_input_tokens), 0).label(
                "cache_creation_input_tokens"
            ),
            func.coalesce(func.sum(TurnOutcome.model_calls), 0).label("model_calls"),
            case((unknown_cost > 0, None), else_=known_cost).label("cost_usd"),
            known_cost.label("known_cost_usd"),
            func.coalesce(func.sum(TurnOutcome.unpriced_calls), 0).label("unpriced_calls"),
        )
        .where(*_filters(tenant_id, since, channel_id, origin))
        .group_by(TurnOutcome.platform, TurnOutcome.channel_id, TurnOutcome.origin)
        .order_by(TurnOutcome.platform, TurnOutcome.channel_id, TurnOutcome.origin)
    )
    rows = (await session.execute(stmt)).mappings()
    return [ChannelUsageRow.model_validate(dict(row)) for row in rows]
