"""Headless MCP turn metering backfill.

Agent-chat `start_turn` (over MCP) creates an MA session and sends a
user.message but never drives the SSE stream, so the live `record_turn_usage`
hook in the turn driver / headless runner never fires — those sessions produce
no usage_events / tenant_ledger rows even though the real Anthropic cost hit
the operator's shared key. This sweep closes that gap out of band: it retrieves
sessions owned by this deployment's database, folds their model-request events,
and replays them through `record_turn_usage`.

Ownership and unsettled usage are registered before billable messages. Recent
local records and explicit interrupted/unobserved turns seed legacy ownership.
The sweep checks recent sessions (two hours) or durable unsettled flags. An idle
session settles only after a complete event read covers its reported token totals.
Old settled histories are not revisited. Finished nonresumable headless sessions
are archived after two hours; live thread mappings and MCP handles are protected.

`record_turn_usage` remains idempotent on (managed_session_id, event_id), the same
grain the live paths write. Only `span.model_request_end` events are requested,
and already-recorded events are skipped. MA requests are paced two minutes apart
(at most 30/hour), including pagination and cleanup. The first 429 ends the pass
and defers later passes for at least `Retry-After`; failed usage progress stays
unsettled. Inline turn usage remains the primary billing path.

Attribution comes off the metadata `create_session` stamps on every session:
`daimon_tenant` is the billed tenant (the tenant_ledger debit keys on it) and
`daimon_account` resolves to the owning human's platform_user_id for per-member
usage reporting. `daimon_budget_channel`, when present, is the channel whose
budget the replayed spend counts toward.

A session stamped `daimon_billing_exempt` was created for a `BillingExempt`
caller (a headless run with no recorder, an MCP caller with no platform user).
Its usage is absorbed by the operator, not debited to the tenant, so the sweep
does not replay it. It still reads the session's events and logs what the
tenant would have been charged (`usage_sweep.exempt_skipped`), and the pass
summary (`usage_sweep.completed`) totals it, so the absorbed spend is visible.
The stamp is written once, at session creation, so the creator's posture
covers every turn on the session: a billed caller continuing an exempt session
is absorbed too, and an exempt caller acting on a billed session is debited.

Candidate API/database errors are recorded and retried without preventing
other sessions from being billed. Rate limits still end the entire pass.
"""

from __future__ import annotations

import uuid
from asyncio import sleep
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import cast

import structlog
from anthropic import APIError, AsyncAnthropic, NotFoundError, RateLimitError, omit
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.sessions import BetaManagedAgentsSessionEvent
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_BILLING_EXEMPT,
    MA_METADATA_KEY_BUDGET_CHANNEL,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.session_mutation import session_mutation_fence
from daimon.core.stores import usage_events, usage_sweep_sessions
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.tenant_balance import debit_amount
from daimon.core.usage_recording import record_turn_usage
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

# At most 30 MA requests/hour, including pagination and session cleanup.
# The scheduler retains the next slot across passes; interactive calls do not
# share this limiter and SDK retries are disabled below.
_REQUEST_INTERVAL_S = 120.0
# Filtered server-side: a session's other events (messages, tool results) are
# most of its history and the sweep never reads them.
_SWEPT_EVENT_TYPES = ["span.model_request_end"]


@dataclass
class UsageSweepWatermark:
    """Pass success, rate-limit deferral and request pacing for this scheduler."""

    last_successful_start: datetime | None = None
    retry_at: datetime | None = None
    next_request_at: float = 0.0


class _ObservedUsage(usage_sweep_sessions.SweepCheckpoint):
    read: bool = False

    def covers(self, session: BetaManagedAgentsSession) -> bool:
        """Keep a durable retry when a session total precedes visible span events."""
        usage = session.usage
        if usage.input_tokens is None or usage.output_tokens is None:
            return False
        cache = usage.cache_creation
        creation = (
            None
            if cache is None
            else (cache.ephemeral_5m_input_tokens or 0) + (cache.ephemeral_1h_input_tokens or 0)
        )
        return self.read and all(
            expected is None or observed >= expected
            for observed, expected in (
                (self.input_tokens, usage.input_tokens),
                (self.output_tokens, usage.output_tokens),
                (self.cache_read_input_tokens, usage.cache_read_input_tokens),
                (self.cache_creation_input_tokens, creation),
            )
        )


@dataclass
class _SweepReads:
    client: AsyncAnthropic
    watermark: UsageSweepWatermark
    sessionmaker: async_sessionmaker[AsyncSession]
    terminal_reason: str | None = None
    model_id: str = ""
    markup: Decimal = Decimal("1")
    calls: int = 0
    observed: _ObservedUsage = field(default_factory=_ObservedUsage)

    async def wait_for_slot(self) -> None:
        wait = self.watermark.next_request_at - monotonic()
        if wait > 0:
            await sleep(wait)

    async def _reserve(self) -> None:
        await self.wait_for_slot()
        self.watermark.next_request_at = monotonic() + _REQUEST_INTERVAL_S
        self.calls += 1

    async def retrieve(self, session_id: str) -> BetaManagedAgentsSession:
        self.terminal_reason = None
        await self._reserve()
        return await self.client.beta.sessions.retrieve(session_id)

    async def archive(self, session_id: str) -> None:
        await self._reserve()
        await self.client.beta.sessions.archive(session_id)

    async def events(self, session_id: str) -> AsyncIterator[BetaManagedAgentsSessionEvent]:
        # The inclusive cursor plus boundary IDs prevents losing events with
        # the same timestamp. A page is checkpointed only after every yielded
        # event was billed successfully; failures replay through idempotency.
        await self._reserve()
        page = await self.client.beta.sessions.events.list(
            session_id,
            order="asc",
            types=_SWEPT_EVENT_TYPES,
            limit=1000,
            created_at_gte=self.observed.cursor or omit,
        )
        seen = set(self.observed.boundary_ids)
        pricing = MODEL_PRICING.get(self.model_id)
        while True:
            for event in page.data:
                if event.id in seen:
                    continue
                seen.add(event.id)
                if event.type != "span.model_request_end":
                    continue
                self.observed.input_tokens += event.model_usage.input_tokens
                self.observed.output_tokens += event.model_usage.output_tokens
                self.observed.cache_read_input_tokens += event.model_usage.cache_read_input_tokens
                self.observed.cache_creation_input_tokens += (
                    event.model_usage.cache_creation_input_tokens
                )
                self.observed.model_calls += 1
                event_cost = cost_of(event.model_usage, pricing)
                self.observed.cost += debit_amount(event_cost, markup=Decimal("1"))
                self.observed.debit += debit_amount(event_cost, markup=self.markup)
                if self.observed.cursor is None or event.processed_at > self.observed.cursor:
                    self.observed.cursor = event.processed_at
                    self.observed.boundary_ids = [event.id]
                elif event.processed_at == self.observed.cursor:
                    self.observed.boundary_ids.append(event.id)
                yield event
            async with self.sessionmaker.begin() as s:
                await usage_sweep_sessions.save_checkpoint(
                    s,
                    session_id=session_id,
                    checkpoint=self.observed,
                )
            if not page.has_next_page():
                self.observed.read = True
                break
            await self._reserve()
            page = await page.get_next_page()


@asynccontextmanager
async def _session_progress(
    sessionmaker: async_sessionmaker[AsyncSession],
    session: BetaManagedAgentsSession,
    started_at: datetime,
    reads: _SweepReads,
    candidate: usage_sweep_sessions.SweepCandidate,
) -> AsyncIterator[None]:
    yield
    complete = reads.observed.covers(session)
    no_progress_reads = 0
    if not complete and reads.observed.read and session.status in ("idle", "terminated"):
        if reads.observed.model_calls <= candidate.checkpoint.model_calls:
            no_progress_reads = candidate.no_progress_reads + 1
        # A bounded full repair also catches late events behind the cursor.
        # It never adds an estimated debit for unobservable tokens.
        reads.observed.rescan = True
        if no_progress_reads >= 3:
            reads.terminal_reason = "coverage_gap"
            log.warning(
                "usage_sweep.coverage_gap",
                session_id=session.id,
                no_progress_reads=no_progress_reads,
            )
    async with sessionmaker.begin() as s:
        if reads.observed.read:
            await usage_sweep_sessions.save_checkpoint(
                s,
                session_id=session.id,
                checkpoint=reads.observed,
            )
        await usage_sweep_sessions.mark_swept(
            s,
            session_id=session.id,
            started_at=started_at,
            activity_at=candidate.activity_at,
            status=session.status,
            archived_at=session.archived_at,
            usage_complete=complete,
            terminal_reason=reads.terminal_reason,
            no_progress_reads=no_progress_reads,
        )
    if (
        candidate.needs_archive
        and session.status in ("idle", "terminated")
        and session.archived_at is None
        and session.metadata.get(MA_METADATA_KEY_TENANT) == str(candidate.tenant_id)
    ):
        # Serialize with MCP sends; re-check durable activity after acquiring
        # the fence so a continuation cannot be archived out from under it.
        # Wait outside the fence: pacing must never block an interactive send.
        await reads.wait_for_slot()
        async with session_mutation_fence(sessionmaker, session.id, check=False):
            async with sessionmaker() as s:
                permitted = await usage_sweep_sessions.can_archive(
                    s, session_id=session.id, now=started_at
                )
            if permitted:
                await reads.archive(session.id)
                async with sessionmaker.begin() as s:
                    await usage_sweep_sessions.mark_archived(
                        s, session_id=session.id, now=started_at
                    )
                log.info("usage_sweep.archived", session_id=session.id)


def _retry_delay(error: RateLimitError, *, now: datetime) -> timedelta:
    retry_ms = error.response.headers.get("retry-after-ms")
    retry = error.response.headers.get("retry-after")
    delay = 60.0
    if retry_ms is not None:
        with suppress(ValueError):
            delay = max(delay, float(retry_ms) / 1000)
    if retry is not None:
        try:
            delay = max(delay, float(retry))
        except ValueError:
            with suppress(ValueError, TypeError, OverflowError):
                retry_date = cast(datetime, parsedate_to_datetime(retry))
                delay = max(delay, (retry_date - now).total_seconds())
    return timedelta(seconds=delay)


@dataclass(frozen=True)
class _AbsorbedUsage:
    """What one exempt session would have cost the tenant."""

    model_calls: int
    cost: Decimal
    debit: Decimal


async def sweep_headless_usage(
    client: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    markup: Decimal,
    watermark: UsageSweepWatermark | None = None,
    now: datetime | None = None,
) -> int:
    """Backfill owned sessions, yielding immediately when MA is rate-limited.

    Recent sessions and durable unsettled flags drive backfill. Settled history
    outside the two-hour continuity window is not polled.
    """
    started_at = now or datetime.now(UTC)
    if watermark is not None and watermark.retry_at is not None and started_at < watermark.retry_at:
        log.info("usage_sweep.deferred", ma_calls=0, retry_at=watermark.retry_at.isoformat())
        return 0
    reads = _SweepReads(
        client.with_options(max_retries=0),
        watermark or UsageSweepWatermark(),
        sessionmaker,
        markup=markup,
    )
    try:
        return await _sweep_owned_usage(
            reads, sessionmaker, markup=markup, watermark=watermark, now=started_at
        )
    except RateLimitError as error:
        rate_limited_at = now or datetime.now(UTC)
        retry_at = rate_limited_at + _retry_delay(error, now=rate_limited_at)
        if watermark is not None:
            watermark.retry_at = retry_at
        log.info("usage_sweep.rate_limited", ma_calls=reads.calls, retry_at=retry_at.isoformat())
        return 0
    finally:
        log.info("usage_sweep.ma_calls", ma_calls=reads.calls)


async def _sweep_owned_usage(
    reads: _SweepReads,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    markup: Decimal,
    watermark: UsageSweepWatermark | None = None,
    now: datetime | None = None,
) -> int:
    """Replay usage with durable per-session progress and unchanged billing semantics."""
    started_at = now or datetime.now(UTC)
    recorded = 0
    exempt_sessions = 0
    exempt_model_calls = 0
    exempt_cost = Decimal("0")
    exempt_debit = Decimal("0")
    async with sessionmaker.begin() as s:
        candidates = await usage_sweep_sessions.list_candidates(s, now=started_at)
    for candidate in candidates:
        try:
            candidate_recorded, absorbed = await _sweep_candidate(
                reads,
                sessionmaker,
                candidate,
                markup=markup,
                started_at=started_at,
            )
        except RateLimitError:
            raise
        except (APIError, SQLAlchemyError) as error:
            # Store only the class: response bodies / SQL parameters can contain
            # credentials or customer data. Use a fresh transaction after rollback.
            error_name = type(error).__name__
            log.warning(
                "usage_sweep.session_failed", session_id=candidate.session_id, error=error_name
            )
            try:
                async with sessionmaker.begin() as s:
                    await usage_sweep_sessions.mark_failed(
                        s,
                        session_id=candidate.session_id,
                        started_at=started_at,
                        error=error_name,
                    )
            except SQLAlchemyError as progress_error:
                log.warning(
                    "usage_sweep.failure_progress_failed",
                    session_id=candidate.session_id,
                    error=type(progress_error).__name__,
                )
            continue
        recorded += candidate_recorded
        if absorbed is not None:
            exempt_sessions += 1
            exempt_model_calls += absorbed.model_calls
            exempt_cost += absorbed.cost
            exempt_debit += absorbed.debit
    if watermark is not None:
        watermark.last_successful_start = started_at
        watermark.retry_at = None
    log.info(
        "usage_sweep.completed",
        ma_calls=reads.calls,
        recorded=recorded,
        exempt_sessions=exempt_sessions,
        exempt_model_calls=exempt_model_calls,
        exempt_cost_usd=str(exempt_cost),
        exempt_would_be_debit_usd=str(exempt_debit),
    )
    return recorded


async def _sweep_candidate(
    reads: _SweepReads,
    sessionmaker: async_sessionmaker[AsyncSession],
    candidate: usage_sweep_sessions.SweepCandidate,
    *,
    markup: Decimal,
    started_at: datetime,
) -> tuple[int, _AbsorbedUsage | None]:
    recorded = 0
    reads.observed = (
        _ObservedUsage()
        if candidate.checkpoint.rescan
        else _ObservedUsage.model_validate(candidate.checkpoint.model_dump())
    )
    try:
        session = await reads.retrieve(candidate.session_id)
    except NotFoundError:
        # A retired/deleted session cannot be reconciled. Advance this
        # candidate so it cannot starve other pending usage.
        async with sessionmaker.begin() as s:
            await usage_sweep_sessions.mark_swept(
                s,
                session_id=candidate.session_id,
                started_at=started_at,
                activity_at=candidate.activity_at,
                status="deleted",
                terminal_reason="deleted",
            )
        return 0, None
    reads.model_id = session.agent.model.id
    async with _session_progress(sessionmaker, session, started_at, reads, candidate):
        tenant_raw = session.metadata.get(MA_METADATA_KEY_TENANT)
        if tenant_raw is None:
            reads.terminal_reason = "missing_tenant_metadata"
            log.warning(
                "usage_sweep.session_skipped", session_id=session.id, reason=reads.terminal_reason
            )
            return 0, None
        try:
            tenant_id = uuid.UUID(tenant_raw)
        except ValueError:
            # An invalid tenant tag cannot be attributed safely; skip only this
            # session so it cannot prevent later sessions from being swept.
            log.warning(
                "usage_sweep.session_skipped",
                session_id=session.id,
                reason="invalid_tenant_metadata",
            )
            reads.terminal_reason = "invalid_tenant_metadata"
            return 0, None
        if tenant_id != candidate.tenant_id:
            log.warning(
                "usage_sweep.session_skipped",
                session_id=session.id,
                reason="tenant_metadata_mismatch",
            )
            reads.terminal_reason = "tenant_metadata_mismatch"
            return 0, None
        exempt_reason = session.metadata.get(MA_METADATA_KEY_BILLING_EXEMPT)
        if exempt_reason is not None:
            absorbed = await _log_absorbed_usage(
                reads, session, tenant_id=tenant_id, reason=exempt_reason, markup=markup
            )
            return 0, absorbed
        platform_user_id = await _resolve_platform_user_id(
            sessionmaker,
            session.metadata,
            session_id=session.id,
            expected_tenant_id=tenant_id,
        )
        model_id = session.agent.model.id
        pricing = MODEL_PRICING.get(model_id)
        channel_id = session.metadata.get(MA_METADATA_KEY_BUDGET_CHANNEL)
        async with sessionmaker() as s:
            recorded_ids = await usage_events.list_event_ids_for_session(
                s, managed_session_id=session.id
            )

        async for event in reads.events(session.id):
            if event.type != "span.model_request_end" or event.id in recorded_ids:
                continue
            await record_turn_usage(
                sessionmaker=sessionmaker,
                tenant_id=tenant_id,
                platform_user_id=platform_user_id,
                managed_session_id=session.id,
                model_id=model_id,
                event=event,
                markup=markup,
                pricing=pricing,
                channel_id=channel_id,
            )
            recorded += 1
    return recorded, None


async def _log_absorbed_usage(
    reads: _SweepReads,
    session: BetaManagedAgentsSession,
    *,
    tenant_id: uuid.UUID,
    reason: str,
    markup: Decimal,
) -> _AbsorbedUsage:
    """Price an exempt session's model calls without recording them, and log it.

    Prices exactly as `record_turn_usage` would (`cost_of` at the session's
    model, then `debit_amount` with the deployment markup), so `cost_usd` and
    `would_be_debit_usd` are the figures the tenant would have been debited.
    An unknown model prices at zero, as the recorder does; `priced` says so.
    The log is emitted on every pass that reads the session's events: sum it
    per session id, not per line.
    """
    model_id = session.agent.model.id
    pricing = MODEL_PRICING.get(model_id)
    async for _event in reads.events(session.id):
        pass
    observed = reads.observed
    log.info(
        "usage_sweep.exempt_skipped",
        tenant_id=str(tenant_id),
        managed_session_id=session.id,
        reason=reason,
        model_id=model_id,
        priced=pricing is not None,
        model_calls=observed.model_calls,
        input_tokens=observed.input_tokens,
        output_tokens=observed.output_tokens,
        cache_creation_input_tokens=observed.cache_creation_input_tokens,
        cache_read_input_tokens=observed.cache_read_input_tokens,
        cost_usd=str(observed.cost),
        would_be_debit_usd=str(observed.debit),
    )
    return _AbsorbedUsage(
        model_calls=observed.model_calls, cost=observed.cost, debit=observed.debit
    )


async def _resolve_platform_user_id(
    sessionmaker: async_sessionmaker[AsyncSession],
    metadata: dict[str, str],
    *,
    session_id: str,
    expected_tenant_id: uuid.UUID,
) -> str | None:
    """Resolve the owning human's platform_user_id from the session's daimon_account.

    Returns None when the session has no account tag or the account has no
    discord principal — the tenant_ledger debit still fires on tenant_id alone,
    so billing stays correct; only per-member reporting attribution is absent.
    """
    account_raw = metadata.get(MA_METADATA_KEY_ACCOUNT)
    if account_raw is None:
        return None
    try:
        account_id = uuid.UUID(account_raw)
    except ValueError:
        # Account attribution is optional; keep tenant usage billing intact.
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
    if identity.tenant_id != expected_tenant_id:
        log.warning(
            "usage_sweep.member_attribution_omitted",
            session_id=session_id,
            reason="account_tenant_mismatch",
        )
        return None
    return identity.platform_user_id
