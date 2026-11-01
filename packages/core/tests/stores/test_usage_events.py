"""Tests for daimon.core.stores.usage_events — BILL-01."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from daimon.core._models import UsageEvent
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.stores import tenants, usage_events
from daimon.core.stores.domain import TenantRow
from daimon.testing.factories import make_tenant, make_usage_event
from mux.testing.drivers.anthropic.ma_models import ma_model_usage
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


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
        model_usage=ma_model_usage(input_tokens=100, output_tokens=50),
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
    usage = ma_model_usage(input_tokens=100, output_tokens=50)
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


async def test_list_event_ids_for_session_returns_only_that_sessions_events(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    for session_id, event_id in (("s1", "evt_1"), ("s1", "evt_2"), ("s2", "evt_3")):
        await make_usage_event(
            db_session, tenant=tenant, managed_session_id=session_id, event_id=event_id
        )

    ids = await usage_events.list_event_ids_for_session(db_session, managed_session_id="s1")
    missing = await usage_events.list_event_ids_for_session(db_session, managed_session_id="s9")

    assert ids == {"evt_1", "evt_2"}, "only the named session's event ids are returned"
    assert missing == frozenset(), "a session with no recorded usage has no ids"


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
        model_usage=ma_model_usage(input_tokens=1_000_000, output_tokens=0),
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
    assert cost_past == 5.0, "since filter in the past should match all rows"


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
    usage = ma_model_usage(input_tokens=10, output_tokens=5)
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
    usage = ma_model_usage(input_tokens=10, output_tokens=5)
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
        model_usage=ma_model_usage(input_tokens=10, output_tokens=5),
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
    one_million = ma_model_usage(input_tokens=1_000_000, output_tokens=0)
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
    assert cost == 5.0, "tenant cost should exclude rows with NULL platform_user_id"


async def test_cost_for_tenant_since_sums_across_users(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    one_million = ma_model_usage(input_tokens=1_000_000, output_tokens=0)
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
    usage = ma_model_usage(input_tokens=10, output_tokens=5)
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
    usage = ma_model_usage(input_tokens=10, output_tokens=5)
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
    one_million = ma_model_usage(input_tokens=1_000_000, output_tokens=0)
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
    assert out["u1"] == 5.0, "u1 cost should equal repriced single-row cost"


async def test_costs_by_user_in_tenant_since_matches_per_user_cost_helper(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    one_million = ma_model_usage(input_tokens=1_000_000, output_tokens=0)
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
    one_million = ma_model_usage(input_tokens=1_000_000, output_tokens=0)
    # u1 uses opus ($5 / 1M input) and sonnet ($3 / 1M input).
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
    assert out["u1"] == 8.0, "u1 cost should sum opus ($5) + sonnet ($3) across models"


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
    usage = ma_model_usage(input_tokens=10, output_tokens=5)
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
    usage = ma_model_usage(input_tokens=10, output_tokens=5)
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
    usage = ma_model_usage(input_tokens=10, output_tokens=5)
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
            model_usage=ma_model_usage(input_tokens=100, output_tokens=50),
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
            model_usage=ma_model_usage(input_tokens=100, output_tokens=50),
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
        model_usage=ma_model_usage(input_tokens=1_000_000, output_tokens=0),
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
        model_usage=ma_model_usage(input_tokens=1_000_000, output_tokens=0),
        event_id="e2",
    )
    cost_a_after = await usage_events.cost_for_tenant_since(
        db_session, tenant_id=tenant_a.id, since=past
    )
    assert cost_a_after == cost_a, "tenant_b write must not affect tenant_a reads"


async def test_costs_by_user_prices_each_haiku_5_5_request_at_its_own_tier(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    """Two 60k-token prompts stay at the base rate even though together they pass 100k."""
    short = ma_model_usage(input_tokens=60_000, output_tokens=0)
    long = ma_model_usage(input_tokens=200_000, output_tokens=0)
    for event_id, usage in [("evt_a", short), ("evt_b", short), ("evt_c", long)]:
        await usage_events.record(
            db_session,
            tenant_id=tenant_id,
            platform_user_id="u1",
            managed_session_id="s1",
            model="claude-haiku-5-5",
            model_usage=usage,
            event_id=event_id,
        )
    past = datetime.now(UTC) - timedelta(days=1)
    out = await usage_events.costs_by_user_in_tenant_since(
        db_session, tenant_id=tenant_id, since=past
    )
    expected = 120_000 * 0.10 / 1_000_000 + 200_000 * 0.50 / 1_000_000
    assert abs(out["u1"] - expected) < 1e-9, "each request is priced at its own tier"
    total = await usage_events.cost_for_tenant_since(db_session, tenant_id=tenant_id, since=past)
    assert abs(total - out["u1"]) < 1e-9, "per-user report agrees with the tenant total"


async def test_cost_aggregates_equal_the_per_row_priced_sum(db_session: AsyncSession) -> None:
    """The SQL-bucketed totals match pricing every row on its own, across models,
    both long-context tiers, cache tokens, an unpriced model and a NULL user."""
    tenant = await make_tenant(db_session)
    rows = [
        ("u1", "claude-haiku-5-5", 60_000, 900, 30_000, 5_000),
        ("u1", "claude-haiku-5-5", 60_000, 100, 0, 0),
        ("u1", "claude-haiku-5-5", 150_000, 2_000, 0, 40_000),
        ("u1", "claude-opus-4-7", 1_000, 500, 200, 300),
        ("u2", "claude-opus-4-7", 120_000, 7_000, 0, 0),
        ("u2", "not-a-priced-model", 5_000, 5_000, 0, 0),
        (None, "claude-opus-4-7", 9_000, 9_000, 0, 0),
    ]
    for user, model, inp, out, cache_write, cache_read in rows:
        await make_usage_event(
            db_session,
            tenant=tenant,
            platform_user_id=user,
            model=model,
            input_tokens=inp,
            output_tokens=out,
            cache_creation_input_tokens=cache_write,
            cache_read_input_tokens=cache_read,
        )
    past = datetime.now(UTC) - timedelta(days=1)

    def per_row(user: str | None) -> float:
        total = 0.0
        for row in stored:
            if row.platform_user_id is None or (user is not None and row.platform_user_id != user):
                continue
            usage = ma_model_usage(
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cache_creation_input_tokens=row.cache_creation_input_tokens,
                cache_read_input_tokens=row.cache_read_input_tokens,
            )
            total += cost_of(usage, MODEL_PRICING.get(row.model)) or 0.0
        return total

    stored = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    tenant_total = await usage_events.cost_for_tenant_since(
        db_session, tenant_id=tenant.id, since=past
    )
    u1_total = await usage_events.cost_for_user_in_tenant_since(
        db_session, tenant_id=tenant.id, platform_user_id="u1", since=past
    )

    assert tenant_total == pytest.approx(per_row(None), rel=1e-12), (
        "tenant aggregate must equal the per-row priced sum"
    )
    assert u1_total == pytest.approx(per_row("u1"), rel=1e-12), (
        "user aggregate must equal that user's per-row priced sum"
    )
    assert u1_total > 0 and tenant_total > u1_total, "fixture must exercise real costs"


async def test_record_stores_agent_id_and_the_row_reads_it_back(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    await make_usage_event(db_session, tenant=None, agent_id="agent_1")
    await usage_events.record(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="u1",
        managed_session_id="s9",
        model="claude-opus-4-7",
        model_usage=ma_model_usage(input_tokens=1, output_tokens=1),
        event_id="evt_9",
        agent_id="agent_2",
    )
    rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant_id)
    assert [r.agent_id for r in rows] == ["agent_2"], "the row model carries agent_id"


async def _spend(
    session: AsyncSession, tenant_id: uuid.UUID, *, user: str | None, agent: str | None, n: int
) -> None:
    """One $5 Opus call (a million input tokens) for `user` on `agent`."""
    await usage_events.record(
        session,
        tenant_id=tenant_id,
        platform_user_id=user,
        managed_session_id=f"s{n}",
        model="claude-opus-4-7",
        model_usage=ma_model_usage(input_tokens=1_000_000, output_tokens=0),
        event_id=f"evt_{n}",
        agent_id=agent,
    )


async def test_costs_by_agent_for_user_since_splits_one_persons_spend_by_agent(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    await _spend(db_session, tenant_id, user="u1", agent="a1", n=1)
    await _spend(db_session, tenant_id, user="u1", agent="a1", n=2)
    await _spend(db_session, tenant_id, user="u1", agent=None, n=3)
    await _spend(db_session, tenant_id, user="u2", agent="a2", n=4)
    past = datetime.now(UTC) - timedelta(days=1)
    by_agent = await usage_events.costs_by_agent_for_user_since(
        db_session, tenant_id=tenant_id, platform_user_id="u1", since=past
    )
    assert by_agent == {"a1": 10.0, None: 5.0}, "u1's spend by agent, unattributed under None"
    future = datetime.now(UTC) + timedelta(hours=1)
    assert not await usage_events.costs_by_agent_for_user_since(
        db_session, tenant_id=tenant_id, platform_user_id="u1", since=future
    ), "the window excludes rows recorded before it"


async def test_costs_by_agent_in_tenant_since_matches_the_tenant_total(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    await _spend(db_session, tenant_id, user="u1", agent="a1", n=1)
    await _spend(db_session, tenant_id, user="u2", agent="a1", n=2)
    await _spend(db_session, tenant_id, user="u2", agent="a2", n=3)
    await _spend(db_session, tenant_id, user=None, agent="a2", n=4)
    past = datetime.now(UTC) - timedelta(days=1)
    by_agent = await usage_events.costs_by_agent_in_tenant_since(
        db_session, tenant_id=tenant_id, since=past
    )
    total = await usage_events.cost_for_tenant_since(db_session, tenant_id=tenant_id, since=past)
    assert by_agent == {"a1": 10.0, "a2": 5.0}, "unattributed-user rows are left out"
    assert sum(by_agent.values()) == total, "the agents add up to the tenant total"


async def test_cost_for_user_in_tenant_since_with_an_agent_counts_only_that_agent(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    await _spend(db_session, tenant_id, user="u1", agent="a1", n=1)
    await _spend(db_session, tenant_id, user="u1", agent="a2", n=2)
    past = datetime.now(UTC) - timedelta(days=1)
    kwargs = {"tenant_id": tenant_id, "platform_user_id": "u1", "since": past}
    only_a1 = await usage_events.cost_for_user_in_tenant_since(db_session, agent_id="a1", **kwargs)
    everything = await usage_events.cost_for_user_in_tenant_since(db_session, **kwargs)
    assert (only_a1, everything) == (5.0, 10.0), "agent_id narrows the window sum; None is all"


async def test_agent_reads_split_cost_and_turns_and_honour_the_window(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    now = datetime.now(UTC)
    for n, (user, agent, age) in enumerate(
        [
            ("ann", "ag_a", 1),
            ("ann", "ag_a", 1),
            ("ann", "ag_b", 1),
            ("bob", None, 1),
            ("ann", "ag_a", 40),
            (None, "ag_a", 1),
        ]
    ):
        tenant = await make_tenant_ref(db_session, tenant_id)
        row = await make_usage_event(
            db_session,
            tenant=tenant,
            platform_user_id=user,
            managed_session_id=f"s{n}",
            event_id=f"e{n}",
            agent_id=agent,
        )
        await db_session.execute(
            update(UsageEvent)
            .where(UsageEvent.id == row.id)
            .values(occurred_at=now - timedelta(days=age))
        )
    since, until = now - timedelta(days=30), now

    own = await usage_events.turns_by_agent_for_user_since(
        db_session, tenant_id=tenant_id, platform_user_id="ann", since=since
    )
    team = await usage_events.turns_by_agent_in_tenant_since(
        db_session, tenant_id=tenant_id, since=since
    )
    old = await usage_events.turns_by_agent_for_user_since(
        db_session,
        tenant_id=tenant_id,
        platform_user_id="ann",
        since=now - timedelta(days=60),
        until=since,
    )
    cost_own = await usage_events.costs_by_agent_for_user_since(
        db_session, tenant_id=tenant_id, platform_user_id="ann", since=since, until=until
    )
    cost_team = await usage_events.costs_by_agent_in_tenant_since(
        db_session, tenant_id=tenant_id, since=since
    )

    assert own == {"ag_a": 2, "ag_b": 1}, "one person's turns by agent, inside the window"
    assert team == {"ag_a": 2, "ag_b": 1, None: 1}, "NULL-user rows are left out"
    assert old == {"ag_a": 1}, "`until` closes the window"
    assert set(cost_own) == {"ag_a", "ag_b"} and cost_own["ag_a"] == pytest.approx(
        2 * cost_own["ag_b"]
    )
    assert set(cost_team) == {"ag_a", "ag_b", None}


async def make_tenant_ref(db_session: AsyncSession, tenant_id: uuid.UUID) -> TenantRow:
    """The row of a tenant the fixture created."""
    tenant = await tenants.get_tenant(db_session, tenant_id)
    assert tenant is not None
    return tenant
