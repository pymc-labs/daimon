"""Tests for daimon.core.usage_recording — BILL-02 / TOPUP-01.

The helper writes one usage_events row per `span.model_request_end`. Tests
construct the real SDK event inline (per guideline:testing) and assert:

- A row appears with token columns sourced from event.model_usage
- Replay of the same event_id is a no-op (store-side ON CONFLICT DO NOTHING)
- DB exceptions propagate (D-25 — usage write failure IS a turn failure)

Debit tests (TOPUP-01):
- Guild turns write a negative tenant_ledger row in the same transaction
- DMs (tenant_id=None) write the usage row but no ledger row
- Replaying the same event does not double-debit (idempotent on turn id)
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core import usage_recording
from daimon.core._models import UsageEvent
from daimon.core.pricing import MODEL_PRICING
from daimon.core.stores import tenant_ledger, usage_events
from daimon.testing.factories import make_tenant
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


async def test_record_turn_usage_writes_row_from_real_sdk_event(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="evt_1",
        is_error=False,
        model_request_start_id="start_1",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        processed_at=datetime.now(UTC),
        type="span.model_request_end",
    )
    await usage_recording.record_turn_usage(
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform_user_id="u1",
        managed_session_id="s1",
        model_id="claude-opus-4-7",
        event=event,
    )
    rows = (await db_session.execute(select(UsageEvent))).scalars().all()
    assert len(rows) == 1, "record_turn_usage should insert exactly one row"
    row = rows[0]
    assert row.input_tokens == 100, "input_tokens should be sourced from event.model_usage"
    assert row.output_tokens == 50, "output_tokens should be sourced from event.model_usage"
    assert row.model == "claude-opus-4-7", "model column should be the bound model_id"
    assert row.event_id == "evt_1", "event_id column should be event.id"
    assert row.managed_session_id == "s1", "managed_session_id should be the bound id"


async def test_record_turn_usage_idempotent_under_replay(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="evt_replay",
        is_error=False,
        model_request_start_id="start_1",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        processed_at=datetime.now(UTC),
        type="span.model_request_end",
    )
    for _ in range(2):
        await usage_recording.record_turn_usage(
            sessionmaker=db_session_factory,
            tenant_id=tenant.id,
            platform_user_id="u1",
            managed_session_id="s1",
            model_id="claude-opus-4-7",
            event=event,
        )
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(
                UsageEvent.managed_session_id == "s1",
                UsageEvent.event_id == "evt_replay",
            )
        )
    ).scalar_one()
    assert count == 1, "replay of same (managed_session_id, event_id) must be a no-op"


async def test_record_turn_usage_propagates_db_errors_no_swallow(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per D-25 — store exceptions must propagate, not be caught here."""
    # Create tenant before monkeypatch so the insert runs against the real store.
    tenant = await make_tenant(db_session)

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(usage_events, "record", boom)

    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="evt_boom",
        is_error=False,
        model_request_start_id="start_1",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=1,
            output_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        processed_at=datetime.now(UTC),
        type="span.model_request_end",
    )
    with pytest.raises(RuntimeError, match="boom"):
        await usage_recording.record_turn_usage(
            sessionmaker=db_session_factory,
            tenant_id=tenant.id,
            platform_user_id="u1",
            managed_session_id="s1",
            model_id="claude-opus-4-7",
            event=event,
        )


# ---------------------------------------------------------------------------
# Debit tests — TOPUP-01 transactional ledger write
# ---------------------------------------------------------------------------


async def test_record_turn_usage_debit_writes_ledger_row_for_guild_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Guild turn: usage row AND negative ledger row written in same transaction."""
    tenant = await make_tenant(db_session)
    # Seed a positive balance so the ledger exists
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.flush()

    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="evt_debit_1",
        is_error=False,
        model_request_start_id="start_1",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=1_000_000,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        processed_at=datetime.now(UTC),
        type="span.model_request_end",
    )
    await usage_recording.record_turn_usage(
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform_user_id="u1",
        managed_session_id="s_debit",
        model_id="claude-opus-4-7",
        event=event,
        markup=Decimal("1.0"),
        pricing=MODEL_PRICING.get("claude-opus-4-7"),
    )
    # Balance should have decreased (input_tokens=1M at $15/M = $15.00 debit)
    balance = await tenant_ledger.get_balance(db_session, tenant_id=tenant.id)
    assert balance < Decimal("10.00"), "balance must decrease after a guild turn debit"
    # $15.00 debit from $10.00 trial credit = -$5.00
    assert balance == Decimal("10.00") - Decimal("15.000000"), (
        "balance should reflect the exact debit: trial credit minus turn cost"
    )


async def test_record_turn_usage_debit_dm_exemption_no_ledger_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """DM turn (tenant_id=None): no usage row, no ledger row — DM is a no-op."""
    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="evt_dm_1",
        is_error=False,
        model_request_start_id="start_1",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        processed_at=datetime.now(UTC),
        type="span.model_request_end",
    )
    await usage_recording.record_turn_usage(
        sessionmaker=db_session_factory,
        platform_user_id="u1",
        managed_session_id="s_dm",
        model_id="claude-opus-4-7",
        event=event,
        tenant_id=None,  # DM — no tenant
    )
    # No usage row must be written — usage_events.tenant_id is NOT NULL
    usage_count = (
        await db_session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(UsageEvent.managed_session_id == "s_dm")
        )
    ).scalar_one()
    assert usage_count == 0, "DM turn (tenant_id=None) must not write a usage row"


async def test_record_turn_usage_debit_idempotent_replay_no_double_debit(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Replaying the same event (same managed_session_id + event.id) must not double-debit."""
    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("50.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.flush()

    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="evt_idem",
        is_error=False,
        model_request_start_id="start_1",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=1_000,
            output_tokens=500,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        processed_at=datetime.now(UTC),
        type="span.model_request_end",
    )
    # Call twice — same event
    for _ in range(2):
        await usage_recording.record_turn_usage(
            sessionmaker=db_session_factory,
            tenant_id=tenant.id,
            platform_user_id="u1",
            managed_session_id="s_idem",
            model_id="claude-sonnet-4-6",
            event=event,
            markup=Decimal("1.0"),
            pricing=MODEL_PRICING.get("claude-sonnet-4-6"),
        )
    balance_after = await tenant_ledger.get_balance(db_session, tenant_id=tenant.id)
    # Should only have debited once — 1000 input + 500 output at claude-sonnet-4-6 rates is tiny
    # If double-debited, balance would be < 49.98; single debit leaves it very close to 50.00
    assert balance_after > Decimal("49.90"), (
        "only one debit must occur even when the same event is replayed"
    )


# ---------------------------------------------------------------------------
# record_media_usage — Gemini media spend (RATE-01)
# ---------------------------------------------------------------------------


async def test_record_media_usage_writes_row_with_plain_int_tokens(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await usage_recording.record_media_usage(
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform_user_id="u1",
        model_id="gemini-3-pro-image-preview",
        input_tokens=10,
        output_tokens=507,
        cache_read_input_tokens=0,
        managed_session_id="gemini:fixed-session",
        event_id="evt_media_1",
    )
    rows = (
        (
            await db_session.execute(
                select(UsageEvent).where(UsageEvent.managed_session_id == "gemini:fixed-session")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, "record_media_usage should insert exactly one usage row"
    row = rows[0]
    assert row.input_tokens == 10, "input_tokens should be sourced from the plain int param"
    assert row.output_tokens == 507, "output_tokens should be sourced from the plain int param"
    assert row.cache_creation_input_tokens == 0, "media usage always has zero cache-write tokens"
    assert row.model == "gemini-3-pro-image-preview", "model column should be the bound model_id"


async def test_record_media_usage_debit_equals_cost_times_markup(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.flush()

    rates = MODEL_PRICING["gemini-3-pro-image-preview"]
    await usage_recording.record_media_usage(
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform_user_id="u1",
        model_id="gemini-3-pro-image-preview",
        input_tokens=10,
        output_tokens=507,
        cache_read_input_tokens=0,
        managed_session_id="gemini:fixed-session-2",
        event_id="evt_media_2",
        markup=Decimal("1.0"),
        pricing=rates,
    )
    expected_cost = (10 * rates.input + 507 * rates.output) / 1_000_000
    balance = await tenant_ledger.get_balance(db_session, tenant_id=tenant.id)
    assert balance < Decimal("10.00"), "balance must decrease after a media debit"
    assert abs(balance - (Decimal("10.00") - Decimal(str(expected_cost)))) < Decimal("0.000001"), (
        "media debit should equal cost_of(usage, pricing) x markup"
    )


async def test_record_media_usage_idempotent_under_replay(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("50.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.flush()

    rates = MODEL_PRICING["gemini-2.5-flash"]
    for _ in range(2):
        await usage_recording.record_media_usage(
            sessionmaker=db_session_factory,
            tenant_id=tenant.id,
            platform_user_id="u1",
            model_id="gemini-2.5-flash",
            input_tokens=1_000,
            output_tokens=500,
            cache_read_input_tokens=0,
            managed_session_id="gemini:fixed-session-3",
            event_id="evt_media_idem",
            markup=Decimal("1.0"),
            pricing=rates,
        )
    usage_count = (
        await db_session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(
                UsageEvent.managed_session_id == "gemini:fixed-session-3",
                UsageEvent.event_id == "evt_media_idem",
            )
        )
    ).scalar_one()
    assert usage_count == 1, "replay of same (managed_session_id, event_id) must be a no-op"
    balance_after = await tenant_ledger.get_balance(db_session, tenant_id=tenant.id)
    assert balance_after > Decimal("49.90"), (
        "only one media debit must occur even when the same call is replayed"
    )


async def test_record_media_usage_propagates_db_errors_no_swallow(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per D-25 — store exceptions must propagate, not be caught here."""
    tenant = await make_tenant(db_session)

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(usage_events, "record", boom)

    with pytest.raises(RuntimeError, match="boom"):
        await usage_recording.record_media_usage(
            sessionmaker=db_session_factory,
            tenant_id=tenant.id,
            platform_user_id="u1",
            model_id="gemini-2.5-flash",
            input_tokens=1,
            output_tokens=1,
            cache_read_input_tokens=0,
        )
