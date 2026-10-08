"""Headless MCP turn metering backfill.

Agent-chat `start_turn` (over MCP) creates an MA session and sends a
user.message but never drives the SSE stream, so the live `record_turn_usage`
hook in the turn driver / headless runner never fires — those sessions produce
no usage_events / tenant_ledger rows even though the real Anthropic cost hit
the operator's shared key. This sweep closes that gap out of band: it lists MA
sessions, folds each session's `span.model_request_end` events, and replays
them through `record_turn_usage`.

`record_turn_usage` is idempotent on (managed_session_id, event_id) — the same
grain the live paths write. A process-local watermark skips event reads for
sessions unchanged since the previous successful pass (with a 15-minute
overlap); startup and hourly passes read all stamped sessions. Only
`span.model_request_end` events are requested, and events already in
usage_events are skipped before any write, so a pass over metered sessions
costs one query per session, not one transaction per event.
No need to distinguish "headless-only" sessions.

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

Per `guideline:architecture` Error Propagation: this does not swallow
exceptions — the scheduler tick is the boundary that decides a sweep failure
must not kill the loop.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_BILLING_EXEMPT,
    MA_METADATA_KEY_BUDGET_CHANNEL,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.stores import usage_events
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.tenants import list_all_tenant_ids
from daimon.core.tenant_balance import debit_amount
from daimon.core.usage_recording import record_turn_usage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

_OVERLAP = timedelta(minutes=15)
_FULL_PASS_INTERVAL = timedelta(hours=1)
# Filtered server-side: a session's other events (messages, tool results) are
# most of its history and the sweep never reads them.
_SWEPT_EVENT_TYPES = ["span.model_request_end"]


@dataclass
class UsageSweepWatermark:
    """Successful pass starts retained for the scheduler process lifetime."""

    last_successful_start: datetime | None = None
    last_full_start: datetime | None = None


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
    """Fold span.model_request_end events from all tagged MA sessions into usage.

    Returns the number of events replayed through `record_turn_usage`: those
    not yet in usage_events when the session was read (a live writer racing the
    sweep can still make one an idempotent no-op). Sessions with no
    `daimon_tenant` tag are skipped — they aren't billable Daimon turns (e.g.
    DMs or foreign sessions). Sessions stamped `daimon_billing_exempt` are
    skipped too, after logging their would-be cost (see the module docstring).

    Session listing remains complete. After a successful pass, event reads skip
    sessions last updated before its start minus 15 minutes. Startup and hourly
    passes read all stamped sessions; failed passes do not advance the watermark.
    """
    started_at = now or datetime.now(UTC)
    full = (
        watermark is None
        or watermark.last_full_start is None
        or started_at - watermark.last_full_start >= _FULL_PASS_INTERVAL
    )
    cutoff = (
        None
        if full or watermark is None or watermark.last_successful_start is None
        else watermark.last_successful_start - _OVERLAP
    )
    recorded = 0
    exempt_sessions = 0
    exempt_model_calls = 0
    exempt_cost = Decimal("0")
    exempt_debit = Decimal("0")
    async with sessionmaker() as s:
        known_tenants = await list_all_tenant_ids(s)
    async for session in client.beta.sessions.list():
        tenant_raw = session.metadata.get(MA_METADATA_KEY_TENANT)
        if tenant_raw is None:
            continue
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
            continue
        if tenant_id not in known_tenants:
            # Session belongs to a tenant this deployment doesn't own. A shared MA
            # workspace holds sessions from other deployments/evals whose tenant_ids
            # are absent from this DB; recording them violates usage_events' FK and
            # is meaningless (not our tenant to bill). Skip.
            continue
        if cutoff is not None and session.updated_at < cutoff:
            continue
        exempt_reason = session.metadata.get(MA_METADATA_KEY_BILLING_EXEMPT)
        if exempt_reason is not None:
            absorbed = await _log_absorbed_usage(
                client, session, tenant_id=tenant_id, reason=exempt_reason, markup=markup
            )
            exempt_sessions += 1
            exempt_model_calls += absorbed.model_calls
            exempt_cost += absorbed.cost
            exempt_debit += absorbed.debit
            continue
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

        async for event in client.beta.sessions.events.list(
            session.id, order="asc", types=_SWEPT_EVENT_TYPES
        ):
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
    if watermark is not None:
        watermark.last_successful_start = started_at
        if full:
            watermark.last_full_start = started_at
    log.info(
        "usage_sweep.completed",
        recorded=recorded,
        exempt_sessions=exempt_sessions,
        exempt_model_calls=exempt_model_calls,
        exempt_cost_usd=str(exempt_cost),
        exempt_would_be_debit_usd=str(exempt_debit),
    )
    return recorded


async def _log_absorbed_usage(
    client: AsyncAnthropic,
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
    model_calls = 0
    input_tokens = 0
    output_tokens = 0
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0
    cost = Decimal("0")
    debit = Decimal("0")
    async for event in client.beta.sessions.events.list(
        session.id, order="asc", types=_SWEPT_EVENT_TYPES
    ):
        if event.type != "span.model_request_end":
            continue
        usage = event.model_usage
        model_calls += 1
        input_tokens += usage.input_tokens
        output_tokens += usage.output_tokens
        cache_creation_input_tokens += usage.cache_creation_input_tokens
        cache_read_input_tokens += usage.cache_read_input_tokens
        event_cost = cost_of(usage, pricing)
        cost += debit_amount(event_cost, markup=Decimal("1"))
        debit += debit_amount(event_cost, markup=markup)
    log.info(
        "usage_sweep.exempt_skipped",
        tenant_id=str(tenant_id),
        managed_session_id=session.id,
        reason=reason,
        model_id=model_id,
        priced=pricing is not None,
        model_calls=model_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cost_usd=str(cost),
        would_be_debit_usd=str(debit),
    )
    return _AbsorbedUsage(model_calls=model_calls, cost=cost, debit=debit)


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
