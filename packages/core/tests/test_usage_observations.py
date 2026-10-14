"""Neutral accounting preserves historical rows and transactional debits."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
from daimon.core.pricing import MODEL_PRICING, cost_of, usage_tokens
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.tenant_balance import debit_amount
from daimon.core.usage_compat import event_observation
from daimon.core.usage_recording import record_turn_usage
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_model_usage
from mux.contracts.usage import UsageObservation
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def observation(*, tenant_id: str | None = None) -> UsageObservation:
    return event_observation(
        BetaManagedAgentsSpanModelRequestEndEvent(
            id="historical-event",
            type="span.model_request_end",
            is_error=False,
            model_request_start_id="start",
            processed_at=datetime(2026, 9, 29, tzinfo=UTC),
            model_usage=ma_model_usage(
                input_tokens=1000,
                output_tokens=500,
                cache_read_input_tokens=200,
                cache_creation_input_tokens=300,
            ),
        ),
        session_id="historical-session",
        model_id="claude-sonnet-4-6",
        tenant_id=tenant_id,
    )


def test_inclusive_input_prices_disjoint_stages_without_double_counting() -> None:
    usage = observation()
    assert usage.input_tokens == 1500
    assert usage.revision == 1
    assert dict(usage.native_meter) == {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_input_tokens": 200,
        "cache_creation_input_tokens": 300,
        "speed": None,
    }
    assert debit_amount(
        cost_of(usage, MODEL_PRICING["claude-sonnet-4-6"]), markup=Decimal("1")
    ) == Decimal("0.011685")
    assert usage_tokens(usage).input_tokens == 1000


@pytest.mark.parametrize(
    "field", ["input_tokens", "output_tokens", "input_cached_tokens", "input_cache_write_tokens"]
)
def test_unknown_counts_remain_unpriced(field: str) -> None:
    usage = observation().model_copy(update={field: None, "completeness": "partial"})
    assert cost_of(usage, MODEL_PRICING["claude-sonnet-4-6"]) is None
    assert usage_tokens(usage) is None


async def test_replay_native_history_through_dto_adds_zero_rows(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    usage = observation(tenant_id=str(tenant.id))
    tokens = usage_tokens(usage)
    await usage_events.record(
        db_session,
        tenant_id=tenant.id,
        platform_user_id="user",
        managed_session_id=usage.session.id,
        model="claude-sonnet-4-6",
        model_usage=tokens,
        event_id=usage.id,
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("-0.011685"),
        reason="turn_debit",
        idempotency_key="turn:historical-session:historical-event",
        occurred_at=usage.observed_at,
    )
    before_usage = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    before_ledger = await tenant_ledger.list_for_tenant(db_session, tenant_id=tenant.id)
    for _ in range(3):
        await record_turn_usage(
            sessionmaker=db_session_factory,
            tenant_id=tenant.id,
            platform_user_id="user",
            observation=usage,
            pricing=MODEL_PRICING["claude-sonnet-4-6"],
        )
    assert await usage_events.list_for_tenant(db_session, tenant_id=tenant.id) == before_usage
    assert await tenant_ledger.list_for_tenant(db_session, tenant_id=tenant.id) == before_ledger


async def test_usage_insert_rolls_back_when_debit_fails_then_replays_once(
    db_nullpool_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    async with sessions() as session:
        tenant = await make_tenant(session)
        await session.commit()
    usage = observation(tenant_id=str(tenant.id))
    args = dict(
        sessionmaker=sessions,
        tenant_id=tenant.id,
        platform_user_id="user",
        observation=usage,
        pricing=MODEL_PRICING["claude-sonnet-4-6"],
    )

    async def crash(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("crash between usage and debit")

    with monkeypatch.context() as patch:
        patch.setattr(tenant_ledger, "insert_entry", crash)
        with pytest.raises(RuntimeError, match="crash between usage and debit"):
            await record_turn_usage(**args)
    async with sessions() as session:
        assert await usage_events.list_for_tenant(session, tenant_id=tenant.id) == []
        assert await tenant_ledger.list_for_tenant(session, tenant_id=tenant.id) == []
    # A new session factory models restarting the host after the failed transaction.
    args["sessionmaker"] = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    for _ in range(2):
        await record_turn_usage(**args)
    async with sessions() as session:
        rows = await tenant_ledger.list_for_tenant(session, tenant_id=tenant.id)
        assert len(await usage_events.list_for_tenant(session, tenant_id=tenant.id)) == 1
        assert len(rows) == 1
        assert rows[0].delta_usd == Decimal("-0.011685")
        assert rows[0].occurred_at == usage.observed_at


@pytest.mark.parametrize(
    "update, message",
    [
        ({"revision": 2}, "accounting outbox"),
        ({"grain": "session"}, "incremental model-request"),
        ({"basis": "cumulative"}, "incremental model-request"),
        ({"input_cached_tokens": None}, "reported token stages"),
    ],
)
async def test_nonbillable_observation_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    update: dict[str, object],
    message: str,
) -> None:
    tenant = await make_tenant(db_session)
    with pytest.raises(ValueError, match=message):
        await record_turn_usage(
            sessionmaker=db_session_factory,
            tenant_id=tenant.id,
            platform_user_id="user",
            observation=observation(tenant_id=str(tenant.id)).model_copy(update=update),
        )
    assert await usage_events.list_for_tenant(db_session, tenant_id=tenant.id) == []
    assert await tenant_ledger.list_for_tenant(db_session, tenant_id=tenant.id) == []


@pytest.mark.parametrize(
    "bindings, message",
    [
        ({"managed_session_id": "other-session"}, "another session"),
        ({"model_id": "other-model"}, "another model"),
    ],
)
async def test_bound_identity_mismatch_cannot_write_usage_or_ledger(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    bindings: dict[str, str],
    message: str,
) -> None:
    tenant = await make_tenant(db_session)
    with pytest.raises(ValueError, match=message):
        await record_turn_usage(
            sessionmaker=db_session_factory,
            tenant_id=tenant.id,
            platform_user_id="user",
            observation=observation(tenant_id=str(tenant.id)),
            **bindings,
        )
    assert await usage_events.list_for_tenant(db_session, tenant_id=tenant.id) == []
    assert await tenant_ledger.list_for_tenant(db_session, tenant_id=tenant.id) == []


async def test_foreign_tenant_observation_cannot_write_usage_or_ledger(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    with pytest.raises(ValueError, match="another tenant"):
        await record_turn_usage(
            sessionmaker=db_session_factory,
            tenant_id=tenant.id,
            platform_user_id="user",
            observation=observation(tenant_id="foreign-tenant"),
        )
    assert await usage_events.list_for_tenant(db_session, tenant_id=tenant.id) == []
    assert await tenant_ledger.list_for_tenant(db_session, tenant_id=tenant.id) == []


def test_cache_stages_cannot_exceed_inclusive_input() -> None:
    with pytest.raises(ValueError, match="exceeds inclusive input"):
        usage_tokens(observation().model_copy(update={"input_tokens": 1}))


@pytest.mark.parametrize(
    "missing, expected",
    [
        ("output_tokens", (1000, None, 200, 300)),
        ("input_tokens", (None, 500, 200, 300)),
        ("input_cached_tokens", (None, 500, None, 300)),
        ("input_cache_write_tokens", (None, 500, 200, None)),
    ],
)
async def test_partial_telemetry_preserves_each_reported_stage(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    missing: str,
    expected: tuple[int | None, ...],
) -> None:
    from daimon.core.stores import turn_outcomes
    from daimon.core.turn.outcomes import TurnObservation, drain_outcomes
    from daimon.core.turn.termination import TerminationReason

    tenant = await make_tenant(db_session)
    await db_session.commit()
    sessions = async_sessionmaker(db_engine)
    usage = observation(tenant_id=str(tenant.id)).model_copy(
        update={missing: None, "completeness": "partial"}
    )
    turn = TurnObservation(sessions, tenant.id, "discord", session_id=usage.session.id)
    turn.note_usage(usage)
    turn.finish(reason=TerminationReason.COMPLETED)
    await drain_outcomes()
    async with sessions() as session:
        rows = await turn_outcomes.list_for_tenant(session, tenant.id)
    assert len(rows) == 1
    row = rows[0]
    assert (
        row.input_tokens,
        row.output_tokens,
        row.cache_read_input_tokens,
        row.cache_creation_input_tokens,
    ) == expected
    assert row.cost_usd is None
    assert row.unpriced_calls == 1
