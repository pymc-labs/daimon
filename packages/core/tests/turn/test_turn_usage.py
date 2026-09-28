"""Usage observations preserve attribution without changing billing."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.turn_usage import list_turn_usage, usage_by_channel
from daimon.core.turn.outcomes import TurnObservation, drain_outcomes
from daimon.core.turn.termination import TerminationReason
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_model_usage
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def span() -> BetaManagedAgentsSpanModelRequestEndEvent:
    return BetaManagedAgentsSpanModelRequestEndEvent(
        id="event",
        is_error=False,
        model_request_start_id="start",
        model_usage=ma_model_usage(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=30,
            cache_creation_input_tokens=40,
        ),
        processed_at=datetime.now(UTC),
        type="span.model_request_end",
    )


@pytest.mark.parametrize("metered", [False, True])
async def test_recovery_usage_deduplicates_per_session_without_billing(
    db_session: AsyncSession, db_engine: AsyncEngine, metered: bool
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    sm = async_sessionmaker(db_engine)
    observation = TurnObservation(sm, tenant.id, "slack", "channel", "thread", "routine")
    for session_id in ("first", "recovered"):
        observation.session_id = session_id
        observation.model_by_session[session_id] = "claude-sonnet-4-6"
        observation.note_usage(span(), metered=metered)
        observation.note_usage(span(), metered=metered)
    observation.finish(reason=TerminationReason.COMPLETED, recovered=True)
    observation.finish()
    await drain_outcomes()
    async with sm() as session:
        rows = await list_turn_usage(
            session, tenant_id=tenant.id, since=datetime.now(UTC) - timedelta(days=1)
        )
        assert await usage_events.list_for_tenant(session, tenant_id=tenant.id) == []
        assert await tenant_ledger.list_for_tenant(session, tenant_id=tenant.id) == []
    assert len(rows) == 1
    row = rows[0]
    assert row.id == observation.id and row.reason == "completed"
    assert (row.channel_id, row.thread_id, row.origin) == ("channel", "thread", "routine")
    assert (row.input_tokens, row.output_tokens) == (200, 40)
    assert (row.cache_read_input_tokens, row.cache_creation_input_tokens) == (60, 80)
    assert row.model_calls == 2 and row.unpriced_calls == 0
    assert row.cost_usd == Decimal("0.001518")
    assert row.model_ids == ["claude-sonnet-4-6"]
    assert row.billing_posture == ("metered" if metered else "exempt")


async def test_queries_scope_and_unknown_costs(
    db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    await db_session.commit()
    sm = async_sessionmaker(db_engine)
    for tenant_id, channel, origin, model in (
        (tenant.id, "one", "chat", "claude-sonnet-4-6"),
        (tenant.id, "one", "chat", "unknown"),
        (tenant.id, "two", "routine", "claude-sonnet-4-6"),
        (other.id, "one", "chat", "claude-sonnet-4-6"),
    ):
        observation = TurnObservation(sm, tenant_id, "discord", channel)
        observation.origin = "routine" if origin == "routine" else "chat"
        observation.session_id = "session"
        observation.model_by_session["session"] = model
        observation.note_usage(span())
        observation.finish(reason=TerminationReason.COMPLETED)
    await drain_outcomes()
    since = datetime.now(UTC) - timedelta(days=1)
    async with sm() as session:
        rows = await list_turn_usage(
            session, tenant_id=tenant.id, since=since, channel_id="one", origin="chat"
        )
        assert len(rows) == 2
        assert sum(row.cost_usd is None for row in rows) == 1
        groups = await usage_by_channel(session, tenant_id=tenant.id, since=since, channel_id="one")
        assert len(groups) == 1
        group = groups[0]
        assert group.turns == group.measured_turns == group.model_calls == 2
        assert group.input_tokens == 200 and group.output_tokens == 40
        assert group.cost_usd is None
        assert group.known_cost_usd == Decimal("0.000759")
        assert group.unpriced_calls == 1
        assert await list_turn_usage(session, tenant_id=tenant.id, since=datetime.now(UTC)) == []
        with pytest.raises(ValueError, match="limit"):
            await list_turn_usage(session, tenant_id=tenant.id, since=since, limit=1001)
