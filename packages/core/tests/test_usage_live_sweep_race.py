"""Postgres behavior when a live turn and MA history sweep record one event."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core import usage_recording
from daimon.core._models import TenantLedger, UsageEvent
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.tenant_balance import debit_amount
from daimon.core.usage_sweep import sweep_headless_usage
from daimon.testing.db import build_test_engine
from daimon.testing.factories import make_platform_principal
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from daimon.testing.ma_models import ma_model_usage, ma_session, ma_session_agent
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool


@pytest_asyncio.fixture
async def usage_race_engine(db_engine: AsyncEngine, db_schema: str) -> AsyncEngine:
    engine = build_test_engine(
        db_engine.url.render_as_string(hide_password=False), db_schema, poolclass=NullPool
    )
    try:
        yield engine
    finally:
        await engine.dispose()


def _event(event_id: str) -> BetaManagedAgentsSpanModelRequestEndEvent:
    return BetaManagedAgentsSpanModelRequestEndEvent(
        id=event_id,
        is_error=False,
        model_request_start_id=f"start_{event_id}",
        model_usage=ma_model_usage(input_tokens=25_000, output_tokens=3_000),
        processed_at=datetime.now(UTC),
        type="span.model_request_end",
    )


async def test_failed_usage_debit_rolls_back_and_retries(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="usage-rollback-user"
    )
    await db_session.commit()

    insert_entry = tenant_ledger.insert_entry

    async def fail_after_insert(
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        delta_usd: Decimal,
        reason: str,
        idempotency_key: str,
        payment_event_id: str | None = None,
        payment_intent: str | None = None,
    ) -> bool:
        await insert_entry(
            session,
            tenant_id=tenant_id,
            delta_usd=delta_usd,
            reason=reason,
            idempotency_key=idempotency_key,
            payment_event_id=payment_event_id,
            payment_intent=payment_intent,
        )
        raise RuntimeError("injected failure after ledger insert")

    monkeypatch.setattr(tenant_ledger, "insert_entry", fail_after_insert)
    event = _event("evt_rollback")
    with pytest.raises(RuntimeError, match="injected failure"):
        await usage_recording.record_turn_usage(
            sessionmaker=db_session_factory,
            tenant_id=principal.tenant_id,
            platform_user_id="usage-rollback-user",
            managed_session_id="sesn_rollback",
            model_id="claude-sonnet-4-6",
            event=event,
        )

    monkeypatch.setattr(tenant_ledger, "insert_entry", insert_entry)
    usage_count = (
        await db_session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(UsageEvent.managed_session_id == "sesn_rollback")
        )
    ).scalar_one()
    debit_count = (
        await db_session.execute(
            select(func.count())
            .select_from(TenantLedger)
            .where(TenantLedger.idempotency_key == "turn:sesn_rollback:evt_rollback")
        )
    ).scalar_one()
    assert usage_count == 0, "failed debit transaction must roll back the usage insert"
    assert debit_count == 0, "failed debit transaction must roll back the ledger insert"

    await usage_recording.record_turn_usage(
        sessionmaker=db_session_factory,
        tenant_id=principal.tenant_id,
        platform_user_id="usage-rollback-user",
        managed_session_id="sesn_rollback",
        model_id="claude-sonnet-4-6",
        event=event,
    )
    rows = (
        await db_session.execute(
            select(UsageEvent, TenantLedger)
            .join(
                TenantLedger,
                TenantLedger.idempotency_key == "turn:sesn_rollback:evt_rollback",
            )
            .where(
                UsageEvent.managed_session_id == "sesn_rollback",
                UsageEvent.event_id == "evt_rollback",
            )
        )
    ).all()
    assert len(rows) == 1, "retry must commit one usage row and matching debit"
    assert rows[0][0].tenant_id == principal.tenant_id, "usage must retain tenant attribution"
    assert rows[0][1].tenant_id == principal.tenant_id, "debit must retain tenant attribution"


async def test_live_writer_and_sweep_wait_on_same_usage_unique_key(
    db_session: AsyncSession,
    usage_race_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="usage-race-user"
    )
    await db_session.commit()
    writer_sessionmaker = async_sessionmaker(bind=usage_race_engine, expire_on_commit=False)
    model_id = "claude-sonnet-4-6"
    event = _event("evt_race")

    session_shape = ma_session(
        id="sesn_race",
        agent=ma_session_agent(id="agent_race", name="race-agent", model=model_id),
        environment_id="env_race",
        metadata={
            "daimon_tenant": str(principal.tenant_id),
            "daimon_account": str(principal.account_id),
        },
        created_at=datetime.now(UTC),
    )
    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions",
        lambda req, m: list_response([session_shape.model_dump(mode="json")]),
    )
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events",
        lambda req, m: list_response([event.model_dump(mode="json")]),
    )
    client = build_fake_anthropic(router.dispatch)

    original_record = usage_events.record
    live_inserted = asyncio.Event()
    release_live = asyncio.Event()
    sweep_backend: int | None = None
    call_count = 0

    async def hold_live_insert(
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        platform_user_id: str | None,
        managed_session_id: str,
        model: str,
        model_usage: BetaManagedAgentsSpanModelUsage,
        event_id: str,
    ) -> None:
        nonlocal call_count, sweep_backend
        call_count += 1
        backend = await session.scalar(text("SELECT pg_backend_pid()"))
        assert backend is not None, "test must have a real Postgres backend"
        if call_count == 1:
            await original_record(
                session,
                tenant_id=tenant_id,
                platform_user_id=platform_user_id,
                managed_session_id=managed_session_id,
                model=model,
                model_usage=model_usage,
                event_id=event_id,
            )
            live_inserted.set()
            await release_live.wait()
            return

        sweep_backend = backend
        await original_record(
            session,
            tenant_id=tenant_id,
            platform_user_id=platform_user_id,
            managed_session_id=managed_session_id,
            model=model,
            model_usage=model_usage,
            event_id=event_id,
        )

    monkeypatch.setattr(usage_events, "record", hold_live_insert)
    live_task: asyncio.Task[None] | None = None
    sweep_task: asyncio.Task[int] | None = None
    try:
        live_task = asyncio.create_task(
            usage_recording.record_turn_usage(
                sessionmaker=writer_sessionmaker,
                tenant_id=principal.tenant_id,
                platform_user_id="usage-race-user",
                managed_session_id="sesn_race",
                model_id=model_id,
                event=event,
                markup=Decimal("1.0"),
                pricing=MODEL_PRICING.get(model_id),
            )
        )
        await asyncio.wait_for(live_inserted.wait(), timeout=10)
        sweep_task = asyncio.create_task(
            sweep_headless_usage(client, writer_sessionmaker, markup=Decimal("1.0"))
        )

        async with asyncio.timeout(10):
            while True:
                if sweep_backend is not None:
                    waiting = await db_session.scalar(
                        text(
                            "SELECT wait_event = 'transactionid' FROM pg_stat_activity "
                            "WHERE pid = :pid"
                        ),
                        {"pid": sweep_backend},
                    )
                    if waiting:
                        break
                await asyncio.sleep(0.01)
        release_live.set()
        swept_count = await sweep_task
        await live_task
        assert swept_count == 1, "sweep should process the shared MA event"
    finally:
        release_live.set()
        if live_task is not None:
            await asyncio.gather(live_task, return_exceptions=True)
        if sweep_task is not None:
            await asyncio.gather(sweep_task, return_exceptions=True)

    usage_rows = (
        (
            await db_session.execute(
                select(UsageEvent).where(
                    UsageEvent.managed_session_id == "sesn_race",
                    UsageEvent.event_id == "evt_race",
                )
            )
        )
        .scalars()
        .all()
    )
    debit_rows = (
        (
            await db_session.execute(
                select(TenantLedger).where(
                    TenantLedger.idempotency_key == "turn:sesn_race:evt_race"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(usage_rows) == 1, "concurrent writers must persist one usage row"
    assert len(debit_rows) == 1, "concurrent writers must persist one debit"
    assert usage_rows[0].tenant_id == principal.tenant_id, "usage must retain tenant attribution"
    assert debit_rows[0].tenant_id == principal.tenant_id, "debit must retain tenant attribution"
    expected_debit = debit_amount(
        cost_of(event.model_usage, MODEL_PRICING.get(model_id)), markup=Decimal("1.0")
    )
    assert debit_rows[0].delta_usd == -expected_debit, (
        "ledger debit must match event price and markup"
    )
