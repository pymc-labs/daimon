"""Per-event usage recording helper.

Callers bind context via `functools.partial(record_turn_usage, ...)` once at
session-create time and pass the resulting callable as `Billed(record=...)`
to `turn.driver.run_turn`'s `billing` parameter, or as the `usage_record`
parameter to `headless_runner.run_turn`. The driver/runner invokes it for
each `span.model_request_end` event.

Per RESEARCH §"SDK Event Shape": the typed SDK event does NOT carry model_id.
The caller resolves it once from `session.agent.model.id`. The neutral path
takes `observation`; the temporary M0 `event` entrypoint converts the SDK
event without provider I/O. Both paths preserve existing stored identities.

Per `guideline:architecture` Error Propagation: exceptions are not
swallowed. A DB failure here IS a turn failure.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.core.pricing import ModelRates, cost_of, usage_tokens
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.tenant_balance import debit_amount
from daimon.core.usage_compat import event_observation
from mux.contracts.ids import ModelRef, ResourceRef
from mux.contracts.usage import UsageObservation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TurnLedgerReason = Literal["turn_debit", "checkpoint_debit"]
"""`tenant_ledger.reason` values a turn-shaped debit may carry.

The column is free text in the schema (no CHECK constraint), so this literal
is the only gate — keep it closed.
"""

SPEND_LEDGER_REASONS: tuple[str, ...] = (
    "turn_debit",
    "checkpoint_debit",
    "media_debit",
    "classifier_debit",
    "thread_naming_debit",
)
"""Every debit reason this module writes: model spend, as opposed to clawbacks
or promo expiries. Timed promo credit is drawn down by exactly these rows, so a
new debit kind added here must be added to this tuple too."""


@dataclass(frozen=True)
class _UsageEventTime:
    """Provider event time under the legacy ledger boundary's field name."""

    processed_at: datetime


async def record_turn_usage(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID | None,
    platform_user_id: str | None,
    managed_session_id: str | None = None,
    model_id: str | None = None,
    observation: UsageObservation | None = None,
    event: BetaManagedAgentsSpanModelRequestEndEvent | None = None,
    markup: Decimal = Decimal("1.0"),
    pricing: ModelRates | None = None,
    reason: TurnLedgerReason = "turn_debit",
    channel_id: str | None = None,
) -> None:
    """Write one usage_events row and one debit ledger row.

    `markup` and `pricing` are keyword-only params for the transactional debit
    (TOPUP-01). Writes a negative delta_usd row to tenant_ledger in the SAME
    transaction as the usage write. The debit is idempotent on
    (managed_session_id, event.id) — mirroring the usage_events dedup grain.

    `reason` names the ledger row's kind. It is a closed literal, not free
    text: a new debit kind is a deliberate, reviewable edit here rather than
    something a caller invents inline. `checkpoint_debit` is the billed
    checkpoint turn a workspace transfer spends on the OLD session
    (`daimon.core.workspace_transfer`) — real model work the tenant pays for,
    but not a turn anyone asked for in a thread, so it is separable in the
    ledger. The idempotency key keeps the `turn:` prefix for every reason:
    it is keyed on (session, event), which is already unique per debit.

    `channel_id` is the turn's parent channel, stamped on both rows so the
    channel's budget counts the debit; None leaves the spend unattributed.

    tenant_id=None is the DM signal — no tenant, no usage row, no ledger row.
    """
    if tenant_id is None:
        return  # DM turn — no tenant to bill, skip write
    if observation is None:
        if event is None or managed_session_id is None or model_id is None:
            raise ValueError("usage requires an observation or a bound legacy event")
        observation = event_observation(
            event,
            session_id=managed_session_id,
            model_id=model_id,
            tenant_id=str(tenant_id),
        )
    elif event is not None:
        raise ValueError("supply either an observation or a legacy event")
    if observation.revision != 1:
        raise ValueError("usage corrections require the accounting outbox")
    if observation.grain != "model_request" or observation.basis != "increment":
        raise ValueError("turn billing requires incremental model-request usage")
    if observation.session.kind != "session":
        raise ValueError("usage observation requires a session reference")
    if observation.session.tenant_id not in (None, str(tenant_id)):
        raise ValueError("usage observation belongs to another tenant")
    if managed_session_id is not None and managed_session_id != observation.session.id:
        raise ValueError("usage observation belongs to another session")
    if observation.model is None:
        if model_id is None:
            raise ValueError("turn billing requires a model")
    elif model_id is not None and observation.model.id != model_id:
        raise ValueError("usage observation belongs to another model")
    else:
        model_id = observation.model.id
    managed_session_id = observation.session.id
    assert model_id is not None
    tokens = usage_tokens(observation)
    if tokens is None:
        raise ValueError("usage rows require all four reported token stages")
    event_time = _UsageEventTime(processed_at=observation.observed_at)
    async with sessionmaker() as s, s.begin():
        await usage_events.record(
            s,
            tenant_id=tenant_id,
            platform_user_id=platform_user_id,
            managed_session_id=managed_session_id,
            model=model_id,
            model_usage=tokens,
            event_id=observation.id,
            channel_id=channel_id,
        )
        cost = cost_of(observation, pricing)
        debit = debit_amount(cost, markup=markup)
        await tenant_ledger.insert_entry(
            s,
            tenant_id=tenant_id,
            delta_usd=-debit,
            reason=reason,
            idempotency_key=f"turn:{managed_session_id}:{observation.id}",
            channel_id=channel_id,
            # The model call's own time, so a debit the sweep writes late still
            # lands inside the timed promo window the call was made in.
            occurred_at=event_time.processed_at,
        )


async def _record_tool_model_usage(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    platform_user_id: str | None,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
    managed_session_id: str | None,
    event_id: str | None,
    markup: Decimal,
    pricing: ModelRates | None,
    reason: str,
    session_prefix: str,
    idempotency_prefix: str,
    channel_id: str | None,
) -> None:
    """One usage_events row plus one debit ledger row for a model call made outside a turn.

    Takes plain ints: callers resolve token counts from their own SDK response
    first. `managed_session_id`/`event_id` default to fresh synthetic ids
    (`{session_prefix}:{uuid4()}` / `uuid4()`), so each call is its own
    billing unit unless the caller threads ids for an idempotency assertion.
    `idempotency_prefix` is separate from `session_prefix` because the ledger
    key is a stored identity: media debits were keyed `media:` before this
    function existed and must keep that prefix.
    Exceptions are NOT swallowed (see module docstring).
    """
    if managed_session_id is None:
        managed_session_id = f"{session_prefix}:{uuid.uuid4()}"
    if event_id is None:
        event_id = str(uuid.uuid4())
    observation = UsageObservation(
        id=event_id,
        revision=1,
        session=ResourceRef(
            id=managed_session_id,
            kind="session",
            provider="gemini" if session_prefix == "gemini" else "anthropic",
            account_scope_id="host-tools",
            tenant_id=str(tenant_id),
        ),
        model=ModelRef(
            provider="gemini" if session_prefix == "gemini" else "anthropic", id=model_id
        ),
        grain="model_request",
        basis="increment",
        input_tokens=input_tokens + cache_read_input_tokens,
        output_tokens=output_tokens,
        input_cache_write_tokens=0,
        input_cached_tokens=cache_read_input_tokens,
        completeness="measured",
        observed_at=datetime.now(UTC),
    )
    tokens = usage_tokens(observation)
    assert tokens is not None
    async with sessionmaker() as s, s.begin():
        await usage_events.record(
            s,
            tenant_id=tenant_id,
            platform_user_id=platform_user_id,
            managed_session_id=managed_session_id,
            model=model_id,
            model_usage=tokens,
            event_id=event_id,
            channel_id=channel_id,
        )
        cost = cost_of(observation, pricing)
        debit = debit_amount(cost, markup=markup)
        await tenant_ledger.insert_entry(
            s,
            tenant_id=tenant_id,
            delta_usd=-debit,
            reason=reason,
            idempotency_key=f"{idempotency_prefix}:{managed_session_id}:{event_id}",
            channel_id=channel_id,
        )


async def record_media_usage(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    platform_user_id: str | None,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
    managed_session_id: str | None = None,
    event_id: str | None = None,
    markup: Decimal = Decimal("1.0"),
    pricing: ModelRates | None = None,
    channel_id: str | None = None,
) -> None:
    """Gemini media spend from the MCP media tools. `daimon.core` never imports `google-genai`."""
    await _record_tool_model_usage(
        sessionmaker=sessionmaker,
        tenant_id=tenant_id,
        platform_user_id=platform_user_id,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        managed_session_id=managed_session_id,
        event_id=event_id,
        markup=markup,
        pricing=pricing,
        reason="media_debit",
        session_prefix="gemini",
        idempotency_prefix="media",
        channel_id=channel_id,
    )


async def record_classifier_usage(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    platform_user_id: str | None,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
    markup: Decimal = Decimal("1.0"),
    pricing: ModelRates | None = None,
    channel_id: str | None = None,
) -> None:
    """The thread-participation classifier call, metered to the tenant like any model spend."""
    await _record_tool_model_usage(
        sessionmaker=sessionmaker,
        tenant_id=tenant_id,
        platform_user_id=platform_user_id,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        managed_session_id=None,
        event_id=None,
        markup=markup,
        pricing=pricing,
        reason="classifier_debit",
        session_prefix="classifier",
        idempotency_prefix="classifier",
        channel_id=channel_id,
    )


async def record_thread_naming_usage(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    platform_user_id: str | None,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
    managed_session_id: str | None = None,
    event_id: str | None = None,
    markup: Decimal = Decimal("1.0"),
    pricing: ModelRates | None = None,
    channel_id: str | None = None,
) -> None:
    """The Haiku call behind an automatic thread title, billed to the message's author."""
    await _record_tool_model_usage(
        sessionmaker=sessionmaker,
        tenant_id=tenant_id,
        platform_user_id=platform_user_id,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        managed_session_id=managed_session_id,
        event_id=event_id,
        markup=markup,
        pricing=pricing,
        reason="thread_naming_debit",
        session_prefix="thread-naming",
        idempotency_prefix="thread-naming",
        channel_id=channel_id,
    )
