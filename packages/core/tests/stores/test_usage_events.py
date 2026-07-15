"""Tests for daimon.core.stores.usage_events — BILL-01."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core._models import UsageEvent
from daimon.core.stores import usage_events
from daimon.testing.factories import make_tenant
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def tenant_id(db_session: AsyncSession) -> uuid.UUID:
    tenant = await make_tenant(db_session)
    return tenant.id


async def test_record_inserts_a_row_with_token_columns_from_model_usage(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        managed_session_id="s1",
        model="claude-opus-4-7",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        event_id="evt_1",
    )
    result = await db_session.execute(select(UsageEvent))
    rows = result.scalars().all()
    assert len(rows) == 1, "record should insert exactly one row"
    row = rows[0]
    assert row.input_tokens == 100, "input_tokens should match model_usage payload"
    assert row.output_tokens == 50, "output_tokens should match model_usage payload"
    assert row.model == "claude-opus-4-7", "model column should match"
    assert row.event_id == "evt_1", "event_id column should match"


async def test_record_idempotent_on_replay(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=100,
        output_tokens=50,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        managed_session_id="s1",
        model="claude-opus-4-7",
        model_usage=usage,
        event_id="evt_1",
    )
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        managed_session_id="s1",
        model="claude-opus-4-7",
        model_usage=usage,
        event_id="evt_1",
    )
    result = await db_session.execute(
        select(func.count())
        .select_from(UsageEvent)
        .where(
            UsageEvent.managed_session_id == "s1",
            UsageEvent.event_id == "evt_1",
        )
    )
    assert result.scalar_one() == 1, (
        "duplicate (managed_session_id, event_id) must be skipped via ON CONFLICT DO NOTHING"
    )


async def test_cost_for_user_in_tenant_sums_against_current_pricing(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        managed_session_id="s1",
        model="claude-opus-4-7",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=1_000_000,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        event_id="evt_1",
    )
    cost = await usage_events.cost_for_user_in_tenant(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
    )
    # claude-opus-4-7 input rate = $15.00 per 1M tokens.
    assert cost == 15.0, "1M input tokens at opus rate should sum to $15.00"


async def test_cost_for_user_in_tenant_since_filters_by_occurred_at(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        managed_session_id="s1",
        model="claude-opus-4-7",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=1_000_000,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        event_id="evt_1",
    )
    # since-in-future returns 0 (filters out the row recorded at now())
    future = datetime.now(UTC) + timedelta(hours=1)
    cost_future = await usage_events.cost_for_user_in_tenant_since(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        since=future,
    )
    assert cost_future == 0.0, "since filter in the future should match no rows"

    # since-in-past returns full cost
    past = datetime.now(UTC) - timedelta(days=1)
    cost_past = await usage_events.cost_for_user_in_tenant_since(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        since=past,
    )
    assert cost_past == 15.0, "since filter in the past should match all rows"


async def test_cost_for_tenant_aggregates_all_users_in_tenant(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    for user_id, event_id in [("u1", "evt_1"), ("u2", "evt_2")]:
        await usage_events.record(
            db_session,
            tenant_id=tenant_id,
            platform_user_id=user_id,
            managed_session_id=f"s_{user_id}",
            model="claude-opus-4-7",
            model_usage=BetaManagedAgentsSpanModelUsage(
                input_tokens=1_000_000,
                output_tokens=0,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            event_id=event_id,
        )
    tenant_cost = await usage_events.cost_for_tenant(
        db_session,
        tenant_id=tenant_id,
    )
    assert tenant_cost == 30.0, "tenant rollup should sum across all users in the tenant"


async def test_turn_count_for_user_in_tenant_since_returns_zero_when_no_rows(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    past = datetime.now(UTC) - timedelta(days=1)
    count = await usage_events.turn_count_for_user_in_tenant_since(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        since=past,
    )
    assert count == 0, "turn count should be 0 when no rows match"


async def test_turn_count_for_user_in_tenant_since_collapses_same_session_across_rows(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    for event_id in ("evt_1", "evt_2", "evt_3"):
        await usage_events.record(
            db_session,
            tenant_id=tenant_id,
            platform_user_id="u1",
            managed_session_id="s1",
            model="claude-opus-4-7",
            model_usage=usage,
            event_id=event_id,
        )
    past = datetime.now(UTC) - timedelta(days=1)
    count = await usage_events.turn_count_for_user_in_tenant_since(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        since=past,
    )
    assert count == 1, (
        "turn count should collapse multiple usage_events rows sharing managed_session_id"
    )


async def test_turn_count_for_user_in_tenant_since_counts_distinct_sessions(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    for session_id in ("s1", "s2", "s3"):
        await usage_events.record(
            db_session,
            tenant_id=tenant_id,
            platform_user_id="u1",
            managed_session_id=session_id,
            model="claude-opus-4-7",
            model_usage=usage,
            event_id=f"evt_{session_id}",
        )
    past = datetime.now(UTC) - timedelta(days=1)
    count = await usage_events.turn_count_for_user_in_tenant_since(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        since=past,
    )
    assert count == 3, "turn count should equal number of distinct managed_session_ids"


async def test_turn_count_for_user_in_tenant_since_filters_period(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        managed_session_id="s1",
        model="claude-opus-4-7",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        event_id="evt_1",
    )
    future = datetime.now(UTC) + timedelta(hours=1)
    count = await usage_events.turn_count_for_user_in_tenant_since(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        since=future,
    )
    assert count == 0, "since filter in the future should exclude existing rows"


async def test_cost_for_tenant_since_excludes_null_user_rows(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    one_million = BetaManagedAgentsSpanModelUsage(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        managed_session_id="s1",
        model="claude-opus-4-7",
        model_usage=one_million,
        event_id="evt_1",
    )
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id=None,
        managed_session_id="s_null",
        model="claude-opus-4-7",
        model_usage=one_million,
        event_id="evt_null",
    )
    past = datetime.now(UTC) - timedelta(days=1)
    cost = await usage_events.cost_for_tenant_since(
        db_session,
        tenant_id=tenant_id,
        since=past,
    )
    assert cost == 15.0, "tenant cost should exclude rows with NULL platform_user_id"


async def test_cost_for_tenant_since_sums_across_users(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    one_million = BetaManagedAgentsSpanModelUsage(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    for user_id in ("u1", "u2"):
        await usage_events.record(
            db_session,
            tenant_id=tenant_id,
            platform_user_id=user_id,
            managed_session_id=f"s_{user_id}",
            model="claude-opus-4-7",
            model_usage=one_million,
            event_id=f"evt_{user_id}",
        )
    past = datetime.now(UTC) - timedelta(days=1)
    tenant_cost = await usage_events.cost_for_tenant_since(
        db_session,
        tenant_id=tenant_id,
        since=past,
    )
    u1_cost = await usage_events.cost_for_user_in_tenant_since(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        since=past,
    )
    u2_cost = await usage_events.cost_for_user_in_tenant_since(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u2",
        since=past,
    )
    assert tenant_cost == u1_cost + u2_cost, "tenant cost should equal sum of per-user costs"


async def test_turn_count_for_tenant_since_excludes_null_user_rows(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        managed_session_id="s1",
        model="claude-opus-4-7",
        model_usage=usage,
        event_id="evt_1",
    )
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id=None,
        managed_session_id="s_null",
        model="claude-opus-4-7",
        model_usage=usage,
        event_id="evt_null",
    )
    past = datetime.now(UTC) - timedelta(days=1)
    count = await usage_events.turn_count_for_tenant_since(
        db_session,
        tenant_id=tenant_id,
        since=past,
    )
    assert count == 1, "tenant turn count should exclude NULL platform_user_id rows"


async def test_turn_count_for_tenant_since_counts_distinct_sessions_across_users(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    for user_id, session_id, event_id in [
        ("u1", "s1", "evt_1"),
        ("u2", "s2", "evt_2"),
    ]:
        await usage_events.record(
            db_session,
            tenant_id=tenant_id,
            platform_user_id=user_id,
            managed_session_id=session_id,
            model="claude-opus-4-7",
            model_usage=usage,
            event_id=event_id,
        )
    past = datetime.now(UTC) - timedelta(days=1)
    count = await usage_events.turn_count_for_tenant_since(
        db_session,
        tenant_id=tenant_id,
        since=past,
    )
    assert count == 2, "tenant turn count should count distinct sessions across users"


async def test_costs_by_user_in_tenant_since_returns_empty_dict_when_no_rows(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    past = datetime.now(UTC) - timedelta(days=1)
    out = await usage_events.costs_by_user_in_tenant_since(
        db_session,
        tenant_id=tenant_id,
        since=past,
    )
    assert out == {}, "costs_by_user should return empty dict when no rows match"


async def test_costs_by_user_in_tenant_since_excludes_null_user_rows(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    one_million = BetaManagedAgentsSpanModelUsage(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        managed_session_id="s1",
        model="claude-opus-4-7",
        model_usage=one_million,
        event_id="evt_1",
    )
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id=None,
        managed_session_id="s_null",
        model="claude-opus-4-7",
        model_usage=one_million,
        event_id="evt_null",
    )
    past = datetime.now(UTC) - timedelta(days=1)
    out = await usage_events.costs_by_user_in_tenant_since(
        db_session,
        tenant_id=tenant_id,
        since=past,
    )
    assert list(out.keys()) == ["u1"], "costs_by_user should exclude NULL platform_user_id"
    assert out["u1"] == 15.0, "u1 cost should equal repriced single-row cost"


async def test_costs_by_user_in_tenant_since_matches_per_user_cost_helper(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    one_million = BetaManagedAgentsSpanModelUsage(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    for user_id, event_id in [("u1", "evt_1"), ("u2", "evt_2")]:
        await usage_events.record(
            db_session,
            tenant_id=tenant_id,
            platform_user_id=user_id,
            managed_session_id=f"s_{user_id}",
            model="claude-opus-4-7",
            model_usage=one_million,
            event_id=event_id,
        )
    past = datetime.now(UTC) - timedelta(days=1)
    out = await usage_events.costs_by_user_in_tenant_since(
        db_session,
        tenant_id=tenant_id,
        since=past,
    )
    for user_id in ("u1", "u2"):
        expected = await usage_events.cost_for_user_in_tenant_since(
            db_session,
            tenant_id=tenant_id,
            platform_user_id=user_id,
            since=past,
        )
        assert out[user_id] == expected, (
            f"costs_by_user[{user_id}] should equal cost_for_user_in_tenant_since({user_id})"
        )


async def test_costs_by_user_in_tenant_since_folds_models_per_user(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    one_million = BetaManagedAgentsSpanModelUsage(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    # u1 uses opus ($15 / 1M input) and sonnet ($3 / 1M input).
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        managed_session_id="s1",
        model="claude-opus-4-7",
        model_usage=one_million,
        event_id="evt_opus",
    )
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        managed_session_id="s2",
        model="claude-sonnet-4-6",
        model_usage=one_million,
        event_id="evt_sonnet",
    )
    past = datetime.now(UTC) - timedelta(days=1)
    out = await usage_events.costs_by_user_in_tenant_since(
        db_session,
        tenant_id=tenant_id,
        since=past,
    )
    assert list(out.keys()) == ["u1"], "single user across multiple models should fold to one entry"
    assert out["u1"] == 18.0, "u1 cost should sum opus ($15) + sonnet ($3) across models"


async def test_turns_by_user_in_tenant_since_returns_empty_dict_when_no_rows(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    past = datetime.now(UTC) - timedelta(days=1)
    out = await usage_events.turns_by_user_in_tenant_since(
        db_session,
        tenant_id=tenant_id,
        since=past,
    )
    assert out == {}, "turns_by_user should return empty dict when no rows match"


async def test_turns_by_user_in_tenant_since_excludes_null_user_rows(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        managed_session_id="s1",
        model="claude-opus-4-7",
        model_usage=usage,
        event_id="evt_1",
    )
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id=None,
        managed_session_id="s_null",
        model="claude-opus-4-7",
        model_usage=usage,
        event_id="evt_null",
    )
    past = datetime.now(UTC) - timedelta(days=1)
    out = await usage_events.turns_by_user_in_tenant_since(
        db_session,
        tenant_id=tenant_id,
        since=past,
    )
    assert list(out.keys()) == ["u1"], "turns_by_user should exclude NULL platform_user_id"
    assert out["u1"] == 1, "u1 should have exactly one turn"


async def test_turns_by_user_in_tenant_since_collapses_same_session_across_models(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    # SAME managed_session_id across two model rows — must collapse to 1 turn.
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        managed_session_id="s1",
        model="claude-opus-4-7",
        model_usage=usage,
        event_id="evt_opus",
    )
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        managed_session_id="s1",
        model="claude-sonnet-4-6",
        model_usage=usage,
        event_id="evt_sonnet",
    )
    past = datetime.now(UTC) - timedelta(days=1)
    out = await usage_events.turns_by_user_in_tenant_since(
        db_session,
        tenant_id=tenant_id,
        since=past,
    )
    assert out["u1"] == 1, (
        "one session producing rows under multiple models must count as one turn — "
        "regression guard against naive sum-over-(user,model) buckets that would yield 2"
    )


async def test_turns_by_user_in_tenant_since_matches_per_user_turn_helper(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    for user_id, session_id, event_id in [
        ("u1", "s_u1_a", "evt_1"),
        ("u1", "s_u1_b", "evt_2"),
        ("u2", "s_u2", "evt_3"),
    ]:
        await usage_events.record(
            db_session,
            tenant_id=tenant_id,
            platform_user_id=user_id,
            managed_session_id=session_id,
            model="claude-opus-4-7",
            model_usage=usage,
            event_id=event_id,
        )
    past = datetime.now(UTC) - timedelta(days=1)
    out = await usage_events.turns_by_user_in_tenant_since(
        db_session,
        tenant_id=tenant_id,
        since=past,
    )
    for user_id in ("u1", "u2"):
        expected = await usage_events.turn_count_for_user_in_tenant_since(
            db_session,
            tenant_id=tenant_id,
            platform_user_id=user_id,
            since=past,
        )
        assert out[user_id] == expected, (
            f"turns_by_user[{user_id}] should equal turn_count_for_user_in_tenant_since({user_id})"
        )


async def test_delete_all_for_user_removes_only_that_users_rows(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    for user_id, event_id in [("u1", "evt_1"), ("u2", "evt_2")]:
        await usage_events.record(
            db_session,
            tenant_id=tenant_id,
            platform_user_id=user_id,
            managed_session_id=f"s_{user_id}",
            model="claude-opus-4-7",
            model_usage=BetaManagedAgentsSpanModelUsage(
                input_tokens=100,
                output_tokens=50,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            event_id=event_id,
        )
    deleted = await usage_events.delete_all_for_user(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
    )
    assert deleted == 1, "delete_all_for_user should return rowcount of deleted rows"

    remaining = await db_session.execute(select(UsageEvent))
    rows = remaining.scalars().all()
    assert len(rows) == 1, "only the targeted user's rows should be removed"
    assert rows[0].platform_user_id == "u2", "the other user's row must remain"


async def test_delete_all_for_user_is_tenant_scoped(
    db_session: AsyncSession,
) -> None:
    """A GDPR purge of user 'U123' in one tenant must not delete another
    tenant's usage rows for an identically-named user (Slack ids collide)."""
    tenant_a = (await make_tenant(db_session)).id
    tenant_b = (await make_tenant(db_session)).id
    for tid in (tenant_a, tenant_b):
        await usage_events.record(
            db_session,
            tenant_id=tid,
            platform_user_id="U123",
            managed_session_id=f"s_{tid}",
            model="claude-opus-4-7",
            model_usage=BetaManagedAgentsSpanModelUsage(
                input_tokens=100,
                output_tokens=50,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            event_id="evt",
        )
    deleted = await usage_events.delete_all_for_user(
        db_session,
        tenant_id=tenant_a,
        platform_user_id="U123",
    )
    assert deleted == 1, "only tenant_a's U123 row should be deleted"

    survivors = await db_session.execute(select(UsageEvent).where(UsageEvent.tenant_id == tenant_b))
    assert len(survivors.scalars().all()) == 1, (
        "another tenant's identically-named user must survive the purge"
    )


# ---------------------------------------------------------------------------
# Cross-tenant isolation test (R-8)
# ---------------------------------------------------------------------------


async def test_usage_events_tenant_isolation(db_session: AsyncSession) -> None:
    # Seed two tenants inline (no shared state between tests)
    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)

    # Write under tenant_a
    await usage_events.record(
        db_session,
        tenant_id=tenant_a.id,
        platform_user_id="u1",
        managed_session_id="s1",
        model="claude-opus-4-7",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=1_000_000,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        event_id="e1",
    )

    past = datetime.min.replace(tzinfo=UTC)

    # Read under tenant_a — sees own row
    cost_a = await usage_events.cost_for_tenant_since(db_session, tenant_id=tenant_a.id, since=past)
    assert cost_a > 0.0, "tenant_a should see its own usage cost"

    # Read under tenant_b — sees nothing
    cost_b = await usage_events.cost_for_tenant_since(db_session, tenant_id=tenant_b.id, since=past)
    assert cost_b == 0.0, "tenant_b must not see tenant_a's usage"

    # Write under tenant_b, re-read tenant_a — unchanged
    await usage_events.record(
        db_session,
        tenant_id=tenant_b.id,
        platform_user_id="u2",
        managed_session_id="s2",
        model="claude-opus-4-7",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=1_000_000,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        event_id="e2",
    )
    cost_a_after = await usage_events.cost_for_tenant_since(
        db_session, tenant_id=tenant_a.id, since=past
    )
    assert cost_a_after == cost_a, "tenant_b write must not affect tenant_a reads"
