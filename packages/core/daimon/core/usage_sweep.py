"""Settle a Managed Agents session's usage: debit every model call it has no ledger row for.

A turn's live recorder only meters the `span.model_request_end` events its own
stream delivers. Calls made while no stream was attached (a worker that died
mid-turn, a ceiling, an interrupt) and every call of a headless agent-chat
turn (MCP `start_turn`/`continue_turn`/`ask`, which never drive a stream)
reach no recorder. `settle_session` is the one path that bills them: it lists
the session's model calls newest first, skips those already in `usage_events`
(written in the same transaction as the ledger debit, so "has a usage row"
means "has a debit"), and replays the rest through `record_turn_usage`. That
recorder is idempotent on the `turn:<session>:<event>` key, so a call recorded
concurrently by a live stream or another settle is debited once.

Callers:

- a chat turn's end (`turn.run`), with the turn's own recorder;
- the MCP status and transcript reads, once they see a headless turn ended
  (`settle_finished_turn`), with the recorder `session_billing` derives from
  the session's metadata stamps;
- `sweep_headless_usage`, the scheduler drain of headless sends nobody read
  to the end (on unless `DAIMON_SCHEDULER__USAGE_SWEEP_ENABLED=false`);
- `settle_backlog` (`daimon usage settle-backlog`), the operator settle of
  calls made before this path was deployed: debited for an explicit internal
  tenant allow-list, waived for everyone else, a dry run unless applied.

There is no stored scan progress. A settle reads the session's calls (within
a page bound) and debits those without a usage row; one cut short simply
leaves the next settle to read again. No automatic settle reads a call made
before the backlog cutoff, the time this path was deployed; the operator's
backlog settle reads only those.

Attribution comes off the metadata `create_session` stamps on every session:
`daimon_tenant` is the billed tenant, `daimon_account` resolves to the owning
human's platform user, `daimon_budget_channel` is the channel whose budget the
spend counts toward. A session stamped `daimon_billing_exempt` is absorbed by
the operator: it is never debited, and its would-be charge is logged
(`usage_sweep.exempt_skipped`) when it is settled.

Per `guideline:architecture` Error Propagation: these functions do not swallow
exceptions, except `settle_finished_turn`, which runs inside a caller's read
and must never fail it.
"""

from __future__ import annotations

import asyncio
import functools
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, cast

import anthropic as _anthropic
import structlog
from anthropic import AsyncAnthropic
from anthropic._models import construct_type
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.sessions import (
    BetaManagedAgentsSessionEvent,
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_BILLING_EXEMPT,
    MA_METADATA_KEY_BUDGET_CHANNEL,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.model_presets import OPUS, OPUS_FAST, model_choice_id
from daimon.core.mux_backend import managed_agents, platform_scope, resource_ref, resource_scope
from daimon.core.mux_compat import legacy_call, legacy_iter
from daimon.core.pricing import MODEL_PRICING, ModelRates, cost_of
from daimon.core.session_ports_compat import retrieve_session_record
from daimon.core.stores import session_settle_state, turn_outcomes, usage_events
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.tenants import get_tenant
from daimon.core.tenant_balance import debit_amount
from daimon.core.usage_recording import record_turn_usage
from mux.contracts.ids import Scope
from mux.drivers.anthropic import EventPage, EventQuery, SessionTools, session_threads
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    # `daimon.core.turn` imports this module for the turn-end settle.
    from daimon.core.turn.posture import UsageRecorder

log = structlog.get_logger(__name__)

# Bounds for a settle on a caller's path (a chat turn's end, an MCP read).
SETTLE_MAX_PAGES = 10
SETTLE_BUDGET_S = 5.0
_PAGE_SIZE = 100
# The drain settles a whole session: a larger bound than a caller's path.
DRAIN_MAX_PAGES = 100
# An unnamed request this young may belong to a send still in flight.
_SEND_IN_FLIGHT = timedelta(minutes=10)
# Marked sessions one drain pass settles.
_DRAIN_BATCH = 50
# Authorization of a settle read the host already validated: a session stamped
# with a tenant this database holds, or the turn's own admitted session.
_SETTLE_AUTHORIZATION = "usage_sweep:validated_session"


def _platform_settle_scope() -> Scope:
    """For reading a session this deployment named itself, before its stamp is known."""
    return platform_scope(
        "billing settle of a session named by this deployment's own records",
        authorization_id="usage_sweep:settle_request",
    )


def _stamped_scope(session: BetaManagedAgentsSession) -> Scope:
    """The tenant scope of a retrieved session's stamp; platform if it has none."""
    try:
        tenant_id = uuid.UUID(session.metadata.get(MA_METADATA_KEY_TENANT) or "")
    except ValueError:
        return _platform_settle_scope()
    return resource_scope(tenant_id=str(tenant_id), authorization_id=_SETTLE_AUTHORIZATION)


@dataclass(frozen=True)
class _EventPage:
    """Host SDK decoding edge; native paging stays behind SessionTools."""

    data: list[BetaManagedAgentsSessionEvent]
    native: EventPage

    def has_next_page(self) -> bool:
        # Match AsyncPageCursor, including empty pages and empty cursor strings.
        return bool(self.data) and bool(self.native.next_page)

    async def get_next_page(self) -> _EventPage:
        return _event_page(await legacy_call(self.native.continue_page()))


def _event_page(page: EventPage) -> _EventPage:
    return _EventPage(
        data=[
            cast(
                BetaManagedAgentsSessionEvent,
                construct_type(value=item.native, type_=BetaManagedAgentsSessionEvent),
            )
            for item in page.data
        ],
        native=page,
    )


async def _list_events(
    anthropic: AsyncAnthropic, session_id: str, *, scope: Scope, query: EventQuery
) -> _EventPage:
    backend = managed_agents(anthropic, scope=scope, resources=frozenset({("session", session_id)}))
    port = backend.extension(SessionTools, namespace="anthropic.session_tools", version=1)
    return _event_page(
        await legacy_call(
            port.list_events(
                scope, resource_ref(backend, "session", session_id, scope=scope), query
            )
        )
    )


def _thread_tariff(native_agent: object) -> str | None:
    """The tariff of a child thread's agent snapshot: its model, fast mode named."""
    if not isinstance(native_agent, dict):
        return None
    model = cast(dict[str, object], native_agent).get("model")
    if not isinstance(model, dict):
        return None
    model_id = cast(dict[str, object], model).get("id")
    if not isinstance(model_id, str):
        return None
    speed = cast(dict[str, object], model).get("speed")
    return OPUS_FAST if model_id == OPUS and speed == "fast" else model_id


def _record_for_model(record: UsageRecorder, model_id: str | None) -> UsageRecorder:
    """`record`, priced at `model_id` instead of the session's own model.

    Every recorder a settle is given is `record_turn_usage` bound with
    `functools.partial` (`bind_recorder`, `SessionBilling.recorder`); one that is
    not, or a thread whose model is unknown, keeps the session's tariff.
    """
    if model_id is None or not isinstance(record, functools.partial):
        return record
    bound = cast("functools.partial[object]", record)
    if bound.keywords.get("model_id") in (None, model_id):
        return cast("UsageRecorder", bound)
    if MODEL_PRICING.get(model_id) is None:
        log.warning("billing.unpriced_model", model_id=model_id)
    return cast(
        "UsageRecorder",
        functools.partial(
            bound.func,
            *bound.args,
            **{**bound.keywords, "model_id": model_id, "pricing": MODEL_PRICING.get(model_id)},
        ),
    )


@dataclass(frozen=True)
class _ChildSettle:
    scanned: int
    unbilled: tuple[BetaManagedAgentsSpanModelRequestEndEvent, ...]
    debited: int
    debit_usd: Decimal
    archived: int


async def _settle_child_threads(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    session_id: str,
    *,
    scope: Scope,
    record: UsageRecorder | None,
) -> _ChildSettle:
    """Bill a coordinator session's helper threads, then archive the finished ones.

    A helper's model calls are listed under its own thread, never in the
    session's event list, so the session settle above cannot see them. Each
    open child thread's calls are recorded through `record` re-bound to the
    thread's own model; the idempotency key is the session's and the event's,
    so a repeat settle writes nothing twice. A thread read to its end with
    every call recorded, and idle, is archived: archived means billed, and the
    next settle skips it. Read-only (`record=None`) archives nothing.
    A session with no child threads costs one list call.
    """
    backend = managed_agents(anthropic, scope=scope, resources=frozenset({("session", session_id)}))
    port = session_threads(backend)
    ref = resource_ref(backend, "session", session_id, scope=scope)
    scanned = 0
    unbilled: list[BetaManagedAgentsSpanModelRequestEndEvent] = []
    debited = 0
    debit_usd = Decimal(0)
    archived = 0
    threads = [snapshot.native async for snapshot in legacy_iter(port.threads(scope, ref))]
    for thread in threads:
        thread_id = thread.get("id")
        if (
            not isinstance(thread_id, str)
            or thread.get("parent_thread_id") is None
            or thread.get("archived_at") is not None
        ):
            continue
        calls = [
            cast(
                BetaManagedAgentsSpanModelRequestEndEvent,
                construct_type(value=item.native, type_=BetaManagedAgentsSessionEvent),
            )
            async for item in legacy_iter(port.thread_events(scope, ref, thread_id))
            if item.native.get("type") == "span.model_request_end"
        ]
        scanned += len(calls)
        async with sessionmaker() as s:
            recorded = await usage_events.recorded_among(
                s, managed_session_id=session_id, event_ids=[e.id for e in calls]
            )
        fresh = [e for e in calls if e.id not in recorded]
        unbilled.extend(fresh)
        if record is None:
            continue
        thread_record = _record_for_model(record, _thread_tariff(thread.get("agent")))
        for event in fresh:
            if (debit := await thread_record(event=event)) is not None:
                debited += 1
                debit_usd += debit
        if thread.get("status") == "idle":
            await legacy_call(port.archive_thread(scope, ref, thread_id))
            archived += 1
    return _ChildSettle(
        scanned=scanned,
        unbilled=tuple(unbilled),
        debited=debited,
        debit_usd=debit_usd,
        archived=archived,
    )


@dataclass(frozen=True)
class SettleResult:
    """What one settle read and wrote.

    `unbilled` are the calls with no usage row when their page was read (all
    of them reported, none written, for a read-only settle). `debited` and
    `debit_usd` count only the ledger rows this settle inserted. `complete`
    means the listing was read to its end within the page and time bounds.
    """

    scanned: int
    unbilled: tuple[BetaManagedAgentsSpanModelRequestEndEvent, ...]
    debited: int
    debit_usd: Decimal
    complete: bool


async def settle_session(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    session_id: str,
    *,
    record: UsageRecorder | None,
    scope: Scope | None = None,
    max_pages: int | None = SETTLE_MAX_PAGES,
    deadline: float | None = None,
    backlog: bool = False,
    helpers: bool = False,
) -> SettleResult:
    """Debit the session's model calls that have no ledger row yet, newest first.

    `helpers` names a coordinator session (`daimon.core.subagents`): its helper
    threads' calls, which its own event list never shows, are billed too, each
    at its thread's model, and finished helper threads are archived
    (`_settle_child_threads`). Within the same deadline.

    `record` must be the recorder bound to this session (`turn.prepare.
    bind_recorder` or `SessionBilling.recorder`), so the calls are priced at
    the session's frozen model and the deployment markup, exactly as a live
    stream would have. `record=None` reads only: it reports the unbilled calls
    and writes nothing.

    Reads the calls made at or after the backlog cutoff, or with `backlog`
    only those before it (the operator's backlog settle), at most `max_pages`
    pages of 100 (None: no bound) and no later than `deadline` (a
    `time.monotonic()` reading, covering the database work too). There is no
    stored progress: a settle cut short keeps every debit it wrote and reports
    itself incomplete, and the next settle reads the listing again, which is
    safe because a call with a usage row is never recorded twice.

    `scope` authorizes the read: the turn's admitted session scope, or the
    tenant of a validated session stamp. None reads under the platform scope
    for a session this deployment named itself.
    """
    scope = scope if scope is not None else _platform_settle_scope()
    scanned = 0
    unbilled: list[BetaManagedAgentsSpanModelRequestEndEvent] = []
    debited = 0
    debit_usd = Decimal(0)
    complete = False
    timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
    try:
        async with asyncio.timeout(timeout):
            async with sessionmaker() as s:
                cutoff = await session_settle_state.backlog_cutoff(s)
            query = EventQuery(order="desc", limit=_PAGE_SIZE, types=["span.model_request_end"])
            # Unset fields mean SDK omit; never turn them into explicit nulls.
            if cutoff is not None and not backlog:
                query = query.model_copy(update={"created_at_gte": cutoff})
            if cutoff is not None and backlog:
                query = query.model_copy(update={"created_at_lt": cutoff})
            page = await _list_events(anthropic, session_id, scope=scope, query=query)
            pages = 1
            while True:
                calls = [
                    e for e in page.data if isinstance(e, BetaManagedAgentsSpanModelRequestEndEvent)
                ]
                scanned += len(calls)
                async with sessionmaker() as s:
                    recorded = await usage_events.recorded_among(
                        s, managed_session_id=session_id, event_ids=[e.id for e in calls]
                    )
                for event in calls:
                    if event.id in recorded:
                        continue
                    unbilled.append(event)
                    if record is not None and (debit := await record(event=event)) is not None:
                        debited += 1
                        debit_usd += debit
                if not page.has_next_page():
                    complete = True
                    break
                if max_pages is not None and pages >= max_pages:
                    break
                page = await page.get_next_page()
                pages += 1
            if helpers and not backlog:
                children = await _settle_child_threads(
                    anthropic, sessionmaker, session_id, scope=scope, record=record
                )
                scanned += children.scanned
                unbilled.extend(children.unbilled)
                debited += children.debited
                debit_usd += children.debit_usd
                if children.archived:
                    log.info(
                        "billing.helper_threads_settled",
                        managed_session_id=session_id,
                        archived=children.archived,
                        debited=children.debited,
                    )
    except TimeoutError:
        complete = False  # every debit written so far stands
    if unbilled or not complete:
        log.info(
            "billing.settled",
            managed_session_id=session_id,
            scanned=scanned,
            unbilled=len(unbilled),
            debited=debited,
            debit_usd=str(debit_usd),
            complete=complete,
            read_only=record is None,
        )
    return SettleResult(
        scanned=scanned,
        unbilled=tuple(unbilled),
        debited=debited,
        debit_usd=debit_usd,
        complete=complete,
    )


def price(
    calls: Sequence[BetaManagedAgentsSpanModelRequestEndEvent],
    *,
    pricing: ModelRates | None,
    markup: Decimal,
) -> tuple[Decimal, Decimal]:
    """(provider cost, ledger debit) of `calls`, priced as `record_turn_usage` would."""
    cost = Decimal(0)
    debit = Decimal(0)
    for event in calls:
        event_cost = cost_of(event.model_usage, pricing)
        cost += debit_amount(event_cost, markup=Decimal(1))
        debit += debit_amount(event_cost, markup=markup)
    return cost, debit


@dataclass(frozen=True)
class SessionBilling:
    """Who a session's model calls are billed to, read off its metadata stamps."""

    tenant_id: uuid.UUID
    platform_user_id: str | None
    model_id: str
    channel_id: str | None
    exempt_reason: str | None

    @property
    def pricing(self) -> ModelRates | None:
        return MODEL_PRICING.get(self.model_id)

    @property
    def scope(self) -> Scope:
        """The read scope of a session whose stamp names a tenant this database holds."""
        return resource_scope(tenant_id=str(self.tenant_id), authorization_id=_SETTLE_AUTHORIZATION)

    def recorder(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        session_id: str,
        *,
        markup: Decimal,
    ) -> UsageRecorder:
        """The recorder a chat turn on this session would bind."""
        if self.pricing is None:
            log.warning("billing.unpriced_model", model_id=self.model_id, ma_session_id=session_id)
        return functools.partial(
            record_turn_usage,
            sessionmaker=sessionmaker,
            tenant_id=self.tenant_id,
            platform_user_id=self.platform_user_id,
            managed_session_id=session_id,
            model_id=self.model_id,
            markup=markup,
            pricing=self.pricing,
            channel_id=self.channel_id,
        )


async def session_billing(
    sessionmaker: async_sessionmaker[AsyncSession], session: BetaManagedAgentsSession
) -> SessionBilling | None:
    """The session's billing, or None if it is not a session this deployment bills.

    None for a session with no `daimon_tenant` stamp (not a Daimon turn), an
    invalid one, or one naming a tenant this database does not hold (a shared
    workspace holds other deployments' sessions; recording them would violate
    `usage_events`' foreign key and bill no one of ours).
    """
    tenant_raw = session.metadata.get(MA_METADATA_KEY_TENANT)
    if tenant_raw is None:
        return None
    try:
        tenant_id = uuid.UUID(tenant_raw)
    except ValueError:
        log.warning(
            "usage_sweep.session_skipped", session_id=session.id, reason="invalid_tenant_metadata"
        )
        return None
    async with sessionmaker() as s:
        if await get_tenant(s, tenant_id) is None:
            return None
    return SessionBilling(
        tenant_id=tenant_id,
        platform_user_id=await _platform_user_id(
            sessionmaker, session.metadata, session_id=session.id, tenant_id=tenant_id
        ),
        model_id=model_choice_id(session.agent.model),
        channel_id=session.metadata.get(MA_METADATA_KEY_BUDGET_CHANNEL),
        exempt_reason=session.metadata.get(MA_METADATA_KEY_BILLING_EXEMPT),
    )


async def _platform_user_id(
    sessionmaker: async_sessionmaker[AsyncSession],
    metadata: dict[str, str],
    *,
    session_id: str,
    tenant_id: uuid.UUID,
) -> str | None:
    """The owning human's platform user, from the session's `daimon_account` stamp.

    None when the session has no account stamp or the account has no platform
    principal: the ledger debit still lands on the tenant; only per-member
    reporting attribution is absent.
    """
    account_raw = metadata.get(MA_METADATA_KEY_ACCOUNT)
    if account_raw is None:
        return None
    try:
        account_id = uuid.UUID(account_raw)
    except ValueError:
        log.warning(
            "usage_sweep.member_attribution_omitted",
            session_id=session_id,
            reason="invalid_account_metadata",
        )
        return None
    async with sessionmaker() as s:
        identity = await get_account_with_tenant(s, account_id=account_id)
    if identity is None:
        return None
    if identity.tenant_id != tenant_id:
        log.warning(
            "usage_sweep.member_attribution_omitted",
            session_id=session_id,
            reason="account_tenant_mismatch",
        )
        return None
    return identity.platform_user_id


async def settle_stamped_session(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    session: BetaManagedAgentsSession,
    *,
    markup: Decimal,
    max_pages: int | None = SETTLE_MAX_PAGES,
    deadline: float | None = None,
) -> SettleResult | None:
    """Settle a session billed by its metadata stamps; None if it is not ours to bill.

    An exempt session is read only and its would-be charge logged.
    """
    billing = await session_billing(sessionmaker, session)
    if billing is None:
        return None
    scope = billing.scope
    # The session's own snapshot says whether it runs helpers, whatever its agent says now.
    helpers = getattr(session.agent, "multiagent", None) is not None
    if billing.exempt_reason is None:
        return await settle_session(
            anthropic,
            sessionmaker,
            session.id,
            record=billing.recorder(sessionmaker, session.id, markup=markup),
            scope=scope,
            max_pages=max_pages,
            deadline=deadline,
            helpers=helpers,
        )
    result = await settle_session(
        anthropic,
        sessionmaker,
        session.id,
        record=None,
        scope=scope,
        max_pages=max_pages,
        deadline=deadline,
        helpers=helpers,
    )
    cost, debit = price(result.unbilled, pricing=billing.pricing, markup=markup)
    log.info(
        "usage_sweep.exempt_skipped",
        tenant_id=str(billing.tenant_id),
        managed_session_id=session.id,
        reason=billing.exempt_reason,
        model_id=billing.model_id,
        priced=billing.pricing is not None,
        model_calls=len(result.unbilled),
        cost_usd=str(cost),
        would_be_debit_usd=str(debit),
    )
    return result


async def turn_ended(
    anthropic: AsyncAnthropic,
    session_id: str,
    request: session_settle_state.PendingSettle,
    *,
    scope: Scope | None = None,
) -> bool:
    """Whether the turn a settle request waits on is listed as ended.

    True only when the turn's own `user.message` is listed and a
    `session.status_idle` or `session.status_terminated` event is listed after
    it; while the message is not listed yet, False. A bare `idle` status read
    right after a send is the previous turn's, so the status alone is never
    trusted. A request whose send could not name its message (the write after
    the send failed) waits on the first message processed at or after the
    request was written. Walks newest first, at most 10 pages. `scope` is as
    for `settle_session`.
    """
    page = await _list_events(
        anthropic,
        session_id,
        scope=scope if scope is not None else _platform_settle_scope(),
        query=EventQuery(
            order="desc",
            limit=_PAGE_SIZE,
            types=["user.message", "session.status_idle", "session.status_terminated"],
        ),
    )
    ended_after = False  # an end event is listed after the current position
    unnamed_turn: bool | None = None  # for an unnamed request: the oldest candidate's answer
    for _ in range(SETTLE_MAX_PAGES):
        for event in page.data:
            if event.type != "user.message":
                ended_after = True
            elif request.turn_event_id is not None:
                if event.id == request.turn_event_id:
                    return ended_after
            elif event.processed_at is None or event.processed_at >= request.requested_at:
                unnamed_turn = ended_after
            else:
                return bool(unnamed_turn)
        if not page.has_next_page():
            break
        page = await page.get_next_page()
    return bool(unnamed_turn)


async def settle_finished_turn(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    session: BetaManagedAgentsSession,
    *,
    markup: Decimal,
    max_pages: int | None = SETTLE_MAX_PAGES,
    budget_s: float | None = SETTLE_BUDGET_S,
) -> SettleResult | None:
    """Settle a headless send's session once its turn has ended, then clear the request.

    Does nothing unless a settle request is pending and `session` (a status
    read just made) is terminated, or idle with the requested turn listed as
    ended. A request not yet named by its send and younger than ten minutes
    is left alone: its send may still be in flight. The request is cleared
    only when the settle read to the end, and only if it is still the very
    request this call verified (a newer send replaces it), so a settle cut
    short, or one racing a send, leaves it for the next read or the drain.
    `budget_s` bounds the whole call, database work included; on expiry the
    request stays.

    Named boundary: an upstream or database error is logged and the request
    kept, except a rate limit, which propagates so the drain stops its pass
    (the MCP read hook swallows it).
    """
    if session.status not in ("idle", "terminated"):
        return None
    deadline = None if budget_s is None else time.monotonic() + budget_s
    try:
        async with asyncio.timeout(budget_s):
            async with sessionmaker() as s:
                request = await session_settle_state.settle_request(
                    s, managed_session_id=session.id
                )
            if request is None:
                return None
            if (
                request.turn_event_id is None
                and datetime.now(UTC) - request.requested_at < _SEND_IN_FLIGHT
            ):
                return None
            if session.status == "idle" and not await turn_ended(
                anthropic, session.id, request, scope=_stamped_scope(session)
            ):
                return None
            result = await settle_stamped_session(
                anthropic,
                sessionmaker,
                session,
                markup=markup,
                max_pages=max_pages,
                deadline=deadline,
            )
            if result is None or result.complete:
                async with sessionmaker() as s, s.begin():
                    await session_settle_state.clear_settle_request(
                        s, managed_session_id=session.id, request=request
                    )
            return result
    except TimeoutError:
        return None  # out of budget; the request stays for the next read or the drain
    except _anthropic.RateLimitError:
        raise
    except (_anthropic.APIError, SQLAlchemyError):
        log.exception("billing.settle_failed", managed_session_id=session.id)
        return None


async def sweep_headless_usage(
    client: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    markup: Decimal,
) -> int:
    """Settle the headless sends nobody read to the end. Returns the calls debited.

    Visits up to 50 sessions with a pending settle request, least recently
    visited first, and settles each whose turn has ended, reading up to 100
    pages (10,000 calls) a session; one larger than that is logged
    (`usage_sweep.session_incomplete`) and keeps its request. A request left
    in place (a session still running, or one whose read failed) goes to the
    back of the queue, so it never blocks the others; a deleted session's
    request is cleared. Any rate limit ends the pass, for the scheduler to
    retry after its interval.
    """
    async with sessionmaker() as s:
        pending = await session_settle_state.list_settle_requests(s, limit=_DRAIN_BATCH)
    debited = 0
    for session_id in pending:
        try:
            session = await retrieve_session_record(
                client, session_id, scope=_platform_settle_scope()
            )
        except _anthropic.NotFoundError:
            async with sessionmaker() as s, s.begin():
                request = await session_settle_state.settle_request(
                    s, managed_session_id=session_id
                )
                if request is not None:
                    await session_settle_state.clear_settle_request(
                        s, managed_session_id=session_id, request=request
                    )
            continue
        except _anthropic.RateLimitError:
            raise
        except _anthropic.APIError:
            log.exception("usage_sweep.session_failed", managed_session_id=session_id)
        else:
            result = await settle_finished_turn(
                client,
                sessionmaker,
                session,
                markup=markup,
                max_pages=DRAIN_MAX_PAGES,
                budget_s=None,
            )
            if result is not None:
                debited += result.debited
                if not result.complete:
                    log.warning("usage_sweep.session_incomplete", managed_session_id=session_id)
        async with sessionmaker() as s, s.begin():
            await session_settle_state.touch_settle_request(s, managed_session_id=session_id)
    log.info("usage_sweep.completed", pending=len(pending), recorded=debited)
    return debited


class BacklogTotal(BaseModel):
    """One tenant's backlog: debited (an internal tenant) or waived (any other).

    `would_debit_usd` is what the calls cost with markup. `debited_usd` is set
    for an internal tenant when applied (the ledger rows written);
    `waived_usd` for any other tenant, dry run or applied.
    """

    model_config = {"frozen": True}

    tenant_id: uuid.UUID
    kind: Literal["internal", "external"]
    sessions: int = 0
    calls: int = 0
    cost_usd: Decimal = Decimal(0)
    would_debit_usd: Decimal = Decimal(0)
    debited_usd: Decimal | None = None
    waived_usd: Decimal | None = None


class BacklogReceipt(BaseModel):
    """What one backlog settle covered: the cutoff, the mode, and each tenant."""

    model_config = {"frozen": True}

    cutoff: datetime | None
    mode: Literal["dry_run", "apply"]
    tenants: list[BacklogTotal]


async def settle_backlog(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    internal: frozenset[uuid.UUID],
    markup: Decimal,
    session_ids: Sequence[str] = (),
    apply: bool = False,
) -> BacklogReceipt:
    """Settle the backlog: calls made before the cutoff that no ledger row covers.

    Reads the sessions named in `session_ids`, or else every MCP agent-chat
    session a turn outcome names, each from the start of its history up to the
    cutoff, so applying writes exactly the calls a dry run reports; calls after
    the cutoff are ordinary billing. A session of an `internal` tenant is
    debited; any other tenant's backlog is waived: no automatic settle reads
    before the cutoff, so its would-be charge is logged
    (`billing.backlog_waived`) and nothing is debited. A re-run reports waived
    calls again, still charging nothing. Exempt sessions are skipped. Without
    `apply` nothing is written; the totals say what applying would debit and
    waive, per tenant. Returns the receipt the operator keeps as evidence.
    """
    async with sessionmaker() as s:
        cutoff = await session_settle_state.backlog_cutoff(s)
        if not session_ids:
            session_ids = await turn_outcomes.session_ids_for_platform(s, "mcp")
    totals: dict[uuid.UUID, BacklogTotal] = {}
    for session_id in session_ids:
        try:
            session = await retrieve_session_record(
                anthropic, session_id, scope=_platform_settle_scope()
            )
        except _anthropic.NotFoundError:
            continue
        billing = await session_billing(sessionmaker, session)
        if billing is None or billing.exempt_reason is not None:
            continue
        debit = billing.tenant_id in internal and apply
        result = await settle_session(
            anthropic,
            sessionmaker,
            session_id,
            record=billing.recorder(sessionmaker, session_id, markup=markup) if debit else None,
            scope=billing.scope,
            max_pages=None,
            backlog=True,
        )
        backlog = result.unbilled
        if not backlog:
            continue
        cost, would_debit = price(backlog, pricing=billing.pricing, markup=markup)
        kind = "internal" if billing.tenant_id in internal else "external"
        total = totals.get(billing.tenant_id) or BacklogTotal(
            tenant_id=billing.tenant_id,
            kind=kind,
            debited_usd=Decimal(0) if debit else None,
            waived_usd=Decimal(0) if kind == "external" else None,
        )
        totals[billing.tenant_id] = total.model_copy(
            update={
                "sessions": total.sessions + 1,
                "calls": total.calls + len(backlog),
                "cost_usd": total.cost_usd + cost,
                "would_debit_usd": total.would_debit_usd + would_debit,
                "debited_usd": None
                if total.debited_usd is None
                else total.debited_usd + result.debit_usd,
                "waived_usd": None if total.waived_usd is None else total.waived_usd + would_debit,
            }
        )
    if apply:
        for total in totals.values():
            if total.kind == "external":
                log.info(
                    "billing.backlog_waived",
                    tenant_id=str(total.tenant_id),
                    sessions=total.sessions,
                    calls=total.calls,
                    would_be_debit_usd=str(total.waived_usd),
                )
    return BacklogReceipt(
        cutoff=cutoff,
        mode="apply" if apply else "dry_run",
        tenants=sorted(totals.values(), key=lambda t: (t.kind, str(t.tenant_id))),
    )
