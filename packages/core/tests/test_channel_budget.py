"""Channel budgets: request validation, windows, the store, spend and the gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.core import usage_recording
from daimon.core._models import TenantLedger, UsageEvent
from daimon.core.channel_budget import (
    ChannelBudgetError,
    ChannelBudgetStatus,
    budget_period,
    describe_budget,
    get_channel_budget_status,
    is_budget_active,
    is_over_channel_budget,
    parse_budget_spec,
)
from daimon.core.pricing import MODEL_PRICING
from daimon.core.stores import channel_budgets
from daimon.core.stores.domain import BudgetWindow
from daimon.core.stores.tenant_ledger import get_channel_spend
from daimon.testing.factories import make_channel_budget, make_ledger_entry, make_tenant
from daimon.testing.ma_models import ma_model_usage
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = datetime(2026, 7, 15, 12, tzinfo=UTC)


def test_parse_budget_spec_reads_money_and_bounds() -> None:
    spec = parse_budget_spec(
        limit_usd=" 12.50 ",
        window="fixed",
        starts_at="2026-07-01",
        ends_at="2026-07-03T00:00:00+02:00",
    )
    assert spec.limit_usd == Decimal("12.50"), "surrounding space is ignored"
    assert spec.starts_at == datetime(2026, 7, 1, tzinfo=UTC), "no offset reads as UTC"
    assert spec.ends_at == datetime(2026, 7, 2, 22, tzinfo=UTC), "offsets convert to UTC"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit_usd": "5", "window": "weekly"}, "window must be one of"),
        ({"limit_usd": "five", "window": "monthly"}, "dollar amount"),
        ({"limit_usd": "-1", "window": "monthly"}, "nonnegative"),
        ({"limit_usd": "NaN", "window": "monthly"}, "nonnegative"),
        ({"limit_usd": "1.005", "window": "monthly"}, "two decimal places"),
        ({"limit_usd": "5", "window": "fixed", "starts_at": "2026-07-01"}, "both"),
        (
            {
                "limit_usd": "5",
                "window": "fixed",
                "starts_at": "2026-07-02",
                "ends_at": "2026-07-01",
            },
            "before",
        ),
        ({"limit_usd": "5", "window": "total", "ends_at": "2026-07-01"}, "only for a fixed"),
        ({"limit_usd": "5", "window": "monthly", "starts_at": "2026-07-01"}, "no starts_at"),
        ({"limit_usd": "5", "window": "total", "starts_at": "yesterday"}, "ISO 8601"),
    ],
)
def test_parse_budget_spec_refuses_bad_requests(kwargs: dict[str, str], message: str) -> None:
    with pytest.raises(ChannelBudgetError, match=message):
        parse_budget_spec(**kwargs)


async def test_budget_period_and_activity_follow_the_window(db_session: AsyncSession) -> None:
    start, end = _NOW - timedelta(days=1), _NOW + timedelta(days=1)
    monthly = await make_channel_budget(db_session, window="monthly")
    total = await make_channel_budget(db_session, window="total", starts_at=start)
    fixed = await make_channel_budget(db_session, window="fixed", starts_at=start, ends_at=end)

    assert budget_period(monthly, now=_NOW).since == datetime(2026, 7, 1, tzinfo=UTC), (
        "a monthly window starts on the 1st, UTC"
    )
    assert budget_period(total, now=_NOW).since == start, "a total window counts from its start"
    fixed_period = budget_period(fixed, now=_NOW)
    assert (fixed_period.since, fixed_period.until) == (start, end), "a fixed window is its bounds"
    assert is_budget_active(fixed, now=_NOW), "inside a fixed window"
    assert not is_budget_active(fixed, now=end), "a fixed window is half-open"
    assert not is_budget_active(total, now=start - timedelta(seconds=1)), (
        "a total window gates nothing before its start"
    )
    assert is_budget_active(total, now=start), "a total window gates from its start"
    open_ended = await make_channel_budget(db_session, channel_id="chan-2", window="total")
    assert is_budget_active(open_ended, now=_NOW), "a total window with no start always gates"
    assert is_budget_active(monthly, now=_NOW), "a monthly window always gates"
    spent = Decimal("1234.5")
    assert describe_budget(ChannelBudgetStatus(monthly, spent, True)) == (
        "$1,234.50 of $5.00 (monthly)"
    ), "money shows in cents with separators"
    assert describe_budget(ChannelBudgetStatus(fixed, spent, True)).endswith(
        "(2026-07-14 12:00 UTC until 2026-07-16 12:00 UTC)"
    ), "a fixed window's end reads as exclusive"
    assert describe_budget(ChannelBudgetStatus(total, spent, True)).endswith(
        "(since 2026-07-14 12:00 UTC)"
    ), "a started total window counts since its start"
    assert describe_budget(ChannelBudgetStatus(total, spent, False)).endswith(
        "(from 2026-07-14 12:00 UTC)"
    ), "a total window that has not started reads from its start"


async def test_set_replaces_the_budget_and_clear_removes_it(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    first = await make_channel_budget(db_session, tenant=tenant, limit_usd=Decimal("5"))
    await channel_budgets.get_channel_budget(
        db_session, tenant_id=tenant.id, platform=tenant.platform, channel_id="chan-1"
    )
    second = await make_channel_budget(
        db_session, tenant=tenant, limit_usd=Decimal("9.99"), window="total"
    )
    await make_channel_budget(db_session, tenant=tenant, channel_id="chan-0")

    assert second.id == first.id, "one row per channel"
    assert (second.limit_usd, second.window) == (Decimal("9.99"), "total"), "set replaces"
    listed = await channel_budgets.list_channel_budgets(db_session, tenant_id=tenant.id)
    assert [b.channel_id for b in listed] == ["chan-0", "chan-1"], "listed by channel id"
    args = {"tenant_id": tenant.id, "platform": tenant.platform, "channel_id": "chan-1"}
    assert await channel_budgets.delete_channel_budget(db_session, **args), "clear removes it"
    assert not await channel_budgets.delete_channel_budget(db_session, **args), (
        "a second clear finds nothing"
    )
    assert await channel_budgets.get_channel_budget(db_session, **args) is None, "it is gone"


async def test_the_table_refuses_a_fixed_window_without_bounds(db_session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        await make_channel_budget(db_session, window="fixed")


async def test_spend_counts_only_the_channels_debits_in_range(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    since = _NOW - timedelta(days=1)
    for delta, channel, when, owner in [
        (Decimal("-2"), "chan-1", _NOW, tenant),
        (Decimal("-3"), "chan-1", _NOW, tenant),
        (Decimal("50"), "chan-1", _NOW, tenant),  # a credit is not spend
        (Decimal("-7"), "chan-2", _NOW, tenant),
        (Decimal("-11"), None, _NOW, tenant),
        (Decimal("-13"), "chan-1", since - timedelta(seconds=1), tenant),
        (Decimal("-17"), "chan-1", _NOW, other),
    ]:
        await make_ledger_entry(
            db_session, tenant=owner, delta_usd=delta, channel_id=channel, occurred_at=when
        )

    spent = await get_channel_spend(
        db_session, tenant_id=tenant.id, channel_id="chan-1", since=since, until=None
    )
    assert spent == Decimal("5"), "only this tenant's and channel's debits since the start"
    ever = await get_channel_spend(
        db_session, tenant_id=tenant.id, channel_id="chan-1", since=None, until=None
    )
    assert ever == Decimal("18"), "no start counts every debit of the channel"


async def test_no_budget_means_no_gate(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("-100"), channel_id="c")
    await db_session.commit()

    for channel_id in ("c", None):
        assert not await is_over_channel_budget(
            sessionmaker=db_session_factory,
            tenant_id=tenant.id,
            platform=tenant.platform,
            channel_id=channel_id,
            now=_NOW,
        ), f"no budget on {channel_id!r} means no gate"


@pytest.mark.parametrize(
    ("limit", "window", "bounds", "over"),
    [
        (Decimal("5"), "monthly", (None, None), True),
        (Decimal("5.01"), "monthly", (None, None), False),
        (Decimal("0"), "total", (None, None), True),
        (Decimal("5"), "total", (_NOW + timedelta(hours=1), None), False),
        (Decimal("5"), "fixed", (_NOW - timedelta(days=1), _NOW + timedelta(days=1)), True),
        (Decimal("1"), "fixed", (_NOW + timedelta(days=1), _NOW + timedelta(days=2)), False),
    ],
    ids=["reached", "under", "zero-stops", "spend-before-start", "fixed-inside", "fixed-outside"],
)
async def test_the_gate_compares_window_spend_with_the_limit(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    limit: Decimal,
    window: BudgetWindow,
    bounds: tuple[datetime | None, datetime | None],
    over: bool,
) -> None:
    tenant = await make_tenant(db_session)
    await make_ledger_entry(
        db_session, tenant=tenant, delta_usd=Decimal("-5"), channel_id="chan-1", occurred_at=_NOW
    )
    await make_channel_budget(
        db_session,
        tenant=tenant,
        limit_usd=limit,
        window=window,
        starts_at=bounds[0],
        ends_at=bounds[1],
    )
    await db_session.commit()

    gated = await is_over_channel_budget(
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform=tenant.platform,
        channel_id="chan-1",
        now=_NOW,
    )
    assert gated is over, "the gate closes once window spend reaches the limit"
    async with db_session_factory() as s:
        status = await get_channel_budget_status(
            s, tenant_id=tenant.id, platform=tenant.platform, channel_id="chan-1", now=_NOW
        )
    assert status is not None, "the budget reads back"
    assert status.remaining_usd == max(Decimal("0"), limit - status.spent_usd), (
        "remaining is never negative"
    )


async def test_a_budget_is_per_platform(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("-1"), channel_id="c")
    await make_channel_budget(
        db_session, tenant=tenant, platform="slack", channel_id="c", limit_usd=Decimal("0")
    )
    await db_session.commit()
    assert not await is_over_channel_budget(
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="c",
        now=_NOW,
    ), "a Slack budget does not gate the same id on Discord"


async def test_recorders_stamp_the_channel_on_both_rows(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="evt_ch",
        is_error=False,
        model_request_start_id="start_1",
        model_usage=ma_model_usage(input_tokens=1000, output_tokens=10),
        processed_at=_NOW,
        type="span.model_request_end",
    )
    await usage_recording.record_turn_usage(
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform_user_id="u1",
        managed_session_id="s1",
        model_id="claude-opus-4-7",
        event=event,
        pricing=MODEL_PRICING.get("claude-opus-4-7"),
        channel_id="chan-1",
    )
    await usage_recording.record_classifier_usage(
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform_user_id="u1",
        model_id="claude-haiku-4-5",
        input_tokens=10,
        output_tokens=1,
        cache_read_input_tokens=0,
        channel_id="chan-2",
    )

    usage = (await db_session.execute(select(UsageEvent.channel_id))).scalars().all()
    ledger = (await db_session.execute(select(TenantLedger.channel_id))).scalars().all()
    assert sorted(usage) == ["chan-1", "chan-2"], "each usage row carries its channel"
    assert sorted(c for c in ledger if c is not None) == ["chan-1", "chan-2"], (
        "each debit carries its channel"
    )
