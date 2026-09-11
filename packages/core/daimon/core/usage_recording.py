"""Per-event usage recording helper.

Callers bind context via `functools.partial(record_turn_usage, ...)` once at
session-create time and pass the resulting callable as `Billed(record=...)`
to `turn.driver.run_turn`'s `billing` parameter, or as the `usage_record`
parameter to `headless_runner.run_turn`. The driver/runner invokes it for
each `span.model_request_end` event.

Per RESEARCH §"SDK Event Shape": the typed SDK event does NOT carry model_id.
The caller resolves it once from `session.agent.model.id` and binds via
`functools.partial`. This module reads tokens from `event.model_usage`.

Per `guideline:architecture` Error Propagation: exceptions are not
swallowed. A DB failure here IS a turn failure.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core.pricing import ModelRates, cost_of
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.tenant_balance import debit_amount
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def record_turn_usage(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID | None,
    platform_user_id: str | None,
    managed_session_id: str,
    model_id: str,
    event: BetaManagedAgentsSpanModelRequestEndEvent,
    markup: Decimal = Decimal("1.0"),
    pricing: ModelRates | None = None,
) -> None:
    """Write one usage_events row and one debit ledger row.

    `markup` and `pricing` are keyword-only params for the transactional debit
    (TOPUP-01). Writes a negative delta_usd row to tenant_ledger in the SAME
    transaction as the usage write. The debit is idempotent on
    (managed_session_id, event.id) — mirroring the usage_events dedup grain.

    tenant_id=None is the DM signal — no tenant, no usage row, no ledger row.
    """
    if tenant_id is None:
        return  # DM turn — no tenant to bill, skip write
    async with sessionmaker() as s, s.begin():
        await usage_events.record(
            s,
            tenant_id=tenant_id,
            platform_user_id=platform_user_id,
            managed_session_id=managed_session_id,
            model=model_id,
            model_usage=event.model_usage,
            event_id=event.id,
        )
        cost = cost_of(event.model_usage, pricing)
        debit = debit_amount(cost, markup=markup)
        await tenant_ledger.insert_entry(
            s,
            tenant_id=tenant_id,
            delta_usd=-debit,
            reason="turn_debit",
            idempotency_key=f"turn:{managed_session_id}:{event.id}",
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
    model_usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=cache_read_input_tokens,
    )
    async with sessionmaker() as s, s.begin():
        await usage_events.record(
            s,
            tenant_id=tenant_id,
            platform_user_id=platform_user_id,
            managed_session_id=managed_session_id,
            model=model_id,
            model_usage=model_usage,
            event_id=event_id,
        )
        cost = cost_of(model_usage, pricing)
        debit = debit_amount(cost, markup=markup)
        await tenant_ledger.insert_entry(
            s,
            tenant_id=tenant_id,
            delta_usd=-debit,
            reason=reason,
            idempotency_key=f"{idempotency_prefix}:{managed_session_id}:{event_id}",
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
    )
