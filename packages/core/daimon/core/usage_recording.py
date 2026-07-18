"""Per-event usage recording helper.

Callers bind context via `functools.partial(record_turn_usage, ...)` once at
session-create time and pass the resulting callable as the `usage_record`
parameter to `turn.driver.run_turn` / `headless_runner.run_turn`. The driver/
runner invokes it for each `span.model_request_end` event.

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
    session_id: str | None = None,
    markup: Decimal = Decimal("1.0"),
    pricing: ModelRates | None = None,
) -> None:
    """Write one usage_events row and one debit ledger row.

    `session_id` is accepted to match the driver/runner call signature
    (`await usage_record(event=event, session_id=session.id)`) and is
    unused — `managed_session_id` is bound by the caller's partial.

    `markup` and `pricing` are keyword-only params for the transactional debit
    (TOPUP-01). Writes a negative delta_usd row to tenant_ledger in the SAME
    transaction as the usage write. The debit is idempotent on
    (managed_session_id, event.id) — mirroring the usage_events dedup grain.

    tenant_id=None is the DM signal — no tenant, no usage row, no ledger row.
    """
    del session_id  # explicit unused
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
    """Write one usage_events row and one media_debit ledger row for Gemini media spend.

    Sibling to `record_turn_usage`, but takes plain ints instead of an SDK
    event — callers (the MCP media tools) resolve token counts from a
    `google-genai` response before calling this; `daimon.core` never imports
    `google-genai`.

    `managed_session_id`/`event_id` default to fresh synthetic ids
    (`gemini:{uuid4()}` / `uuid4()`) when not supplied, so each call is its
    own billing unit unless the caller explicitly threads ids for a test's
    idempotency assertion.

    `tenant_id` is non-optional — this is only called on the billed path;
    the trusted-path skip (no tenant, no metering) happens adapter-side.

    Per the module docstring: exceptions are NOT swallowed — no try/except.
    """
    if managed_session_id is None:
        managed_session_id = f"gemini:{uuid.uuid4()}"
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
            reason="media_debit",
            idempotency_key=f"media:{managed_session_id}:{event_id}",
        )
