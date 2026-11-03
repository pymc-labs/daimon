"""What the credits page reads: pure helpers, then the reads over real Postgres."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.core import credit_views as views
from daimon.core._models import UsageEvent
from daimon.core.pricing import MODEL_PRICING
from daimon.core.stores import (
    platform_names,
    tenant_ledger,
    turn_outcomes,
    usage_events,
    web_chat,
)
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.platform_names import KnownName
from daimon.core.turn.termination import TerminationReason
from daimon.core.usage_recording import SPEND_LEDGER_REASONS, record_turn_usage
from daimon.testing.factories import (
    make_account,
    make_ledger_entry,
    make_platform_principal,
    make_tenant,
    make_usage_event,
    make_web_person,
)
from mux.testing.drivers.anthropic.ma_models import ma_model_usage
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 3, 16, 12, tzinfo=UTC)
MARKUP = Decimal("2")


def test_billed_applies_the_markup_and_treats_unknown_as_nothing() -> None:
    assert views.billed(0.5, MARKUP) == Decimal("1.000000")
    assert views.billed(None, MARKUP) == Decimal(0), "an unknown cost bills nothing"


def test_recent_window_is_the_last_thirty_days_with_today_last() -> None:
    assert views.period_window("30d", now=NOW) == (datetime(2026, 2, 15, tzinfo=UTC), None)
    early = datetime(2026, 11, 2, 9, tzinfo=UTC)
    assert views.period_window("30d", now=early)[0] == datetime(2026, 10, 4, tzinfo=UTC), (
        "early in a month the window reaches back into the last one"
    )


def test_period_windows_are_calendar_months() -> None:
    assert views.period_window("this", now=NOW) == (datetime(2026, 3, 1, tzinfo=UTC), None)
    assert views.period_window("last", now=NOW) == (
        datetime(2026, 2, 1, tzinfo=UTC),
        datetime(2026, 3, 1, tzinfo=UTC),
    )
    january = datetime(2026, 1, 5, tzinfo=UTC)
    assert views.period_window("last", now=january)[0] == datetime(2025, 12, 1, tzinfo=UTC)
    assert views.next_month_start(datetime(2026, 12, 9, tzinfo=UTC)) == datetime(
        2027, 1, 1, tzinfo=UTC
    ), "December rolls into the next year"


def test_day_bounds_cover_the_last_thirty_days() -> None:
    bounds = views.day_bounds(NOW)
    assert len(bounds) == 31, "30 days plus the end of today"
    assert bounds[0] == datetime(2026, 2, 15, tzinfo=UTC)
    assert bounds[-1] == datetime(2026, 3, 17, tzinfo=UTC)


def test_projection_follows_the_pace_after_the_first_day() -> None:
    first_day = datetime(2026, 3, 1, 6, tzinfo=UTC)
    assert views.projected_month(Decimal(3), now=first_day) == Decimal(3)
    ten_days = datetime(2026, 3, 11, tzinfo=UTC)
    assert views.projected_month(Decimal(10), now=ten_days) == Decimal("31.000000")


@pytest.mark.parametrize(
    ("reason", "by_person", "kind"),
    [
        ("topup", False, "topup"),
        ("manual_credit", True, "topup"),
        ("manual_credit", False, "adjustment"),
        ("promo_credit", False, "promo"),
        ("trial", False, "promo"),
        ("refund", False, "adjustment"),
    ],
)
def test_ledger_kind(reason: str, by_person: bool, kind: str) -> None:
    assert views.ledger_kind(reason, by_person=by_person) == kind


async def _event(
    db_session: AsyncSession,
    tenant: TenantRow,
    *,
    user: str | None,
    agent: str | None,
    at: datetime,
    n: int,
    channel: str | None = None,
    session: str | None = None,
) -> None:
    """One Opus call (1M input tokens, $5 raw) recorded at `at`; `n` makes it distinct."""
    row = await make_usage_event(
        db_session,
        tenant=tenant,
        platform_user_id=user,
        channel_id=channel,
        managed_session_id=session or f"s{n}",
        event_id=f"e{n}",
        model="claude-opus-4-7",
        input_tokens=1_000_000,
        output_tokens=0,
        agent_id=agent,
    )
    await db_session.execute(
        update(UsageEvent).where(UsageEvent.id == row.id).values(occurred_at=at)
    )


async def test_spend_is_billed_grouped_and_windowed(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    this_month, last_month = NOW - timedelta(days=2), NOW - timedelta(days=30)
    for n, (user, agent, at) in enumerate(
        [
            ("ann", "ag_a", this_month),
            ("ann", "ag_a", this_month),
            ("ann", "ag_b", this_month),
            ("bob", "ag_a", this_month),
            ("bob", None, this_month),
            ("ann", "ag_a", last_month),
            (None, "ag_a", this_month),
        ]
    ):
        await _event(db_session, tenant, user=user, agent=agent, at=at, n=n)
    start, _ = views.period_window("this", now=NOW)
    last_start, last_end = views.period_window("last", now=NOW)

    mine = await views.spend_by_agent_for_user(
        db_session, tenant_id=tenant.id, platform_user_id="ann", markup=MARKUP, since=start
    )
    assert mine == [
        views.AgentSpend("ag_a", Decimal("20.000000"), 2),
        views.AgentSpend("ag_b", Decimal("10.000000"), 1),
    ], "billed at the markup, biggest first, this month only"

    team = await views.spend_by_agent_in_tenant(
        db_session, tenant_id=tenant.id, markup=MARKUP, since=start
    )
    assert [(r.agent_id, r.usd) for r in team] == [
        ("ag_a", Decimal("40.000000")),
        (None, Decimal("10.000000")),
        ("ag_b", Decimal("10.000000")),
    ], "agent-less spend is its own row and spend with no person counts on its agent"

    people = await views.spend_by_person(
        db_session, tenant_id=tenant.id, markup=MARKUP, since=start
    )
    assert [(r.user_id, r.usd, r.turns) for r in people] == [
        ("ann", Decimal("30.000000"), 3),
        ("bob", Decimal("20.000000"), 2),
        (None, Decimal("10.000000"), 1),
    ], "spend with no person is a row of its own"

    last = await views.spend_by_person(
        db_session, tenant_id=tenant.id, markup=MARKUP, since=last_start, until=last_end
    )
    assert [(r.user_id, r.usd) for r in last] == [("ann", Decimal("10.000000"))], (
        "last month holds only last month's call"
    )


async def _outcome(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    account_id: uuid.UUID | None,
    platform: str,
    channel: str | None,
    thread: str | None,
    cost: Decimal | None,
    at: datetime,
    agent: str | None = "ag_a",
    origin: str = "chat",
    session: str | None = None,
) -> uuid.UUID:
    outcome_id = uuid.uuid4()
    await turn_outcomes.record(
        db_session,
        turn_outcomes.OutcomeRecord(
            id=outcome_id,
            tenant_id=tenant_id,
            account_id=account_id,
            platform=platform,
            channel_id=channel,
            thread_id=thread,
            agent_id=agent,
            session_id=session or f"sess-{outcome_id}",
            origin=origin,  # pyright: ignore[reportArgumentType]
            reason=TerminationReason.COMPLETED,
            started_at=at,
            ended_at=at,
            duration_ms=1,
            recovered=False,
            error_class=None,
            release="test",
            usage_refs=[],
            model_calls=1 if cost is not None else None,
            cost_usd=cost,
        ),
    )
    return outcome_id


async def test_channel_spend_and_recent_turns_carry_names_and_titles(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    other = await make_account(db_session, tenant=tenant)
    person = await make_web_person(db_session, tenant=tenant, account=account)
    thread = await web_chat.create_thread(
        db_session,
        tenant_id=tenant.id,
        person_id=person.id,
        agent_id="ag_a",
        agent_name="daimon",
        title="Quarterly plan",
    )
    await make_platform_principal(
        db_session, platform="discord", external_id="d1", tenant=tenant, account=account
    )
    await platform_names.upsert_channel_names(
        db_session, tenant_id=tenant.id, platform="discord", names={"c1": "general"}
    )
    since = NOW - timedelta(days=10)
    web = await _outcome(
        db_session, tenant.id, account_id=account.id, platform="web", channel=None,
        thread=f"web:{thread.id}", cost=Decimal("1.5"), at=NOW - timedelta(hours=3),
    )  # fmt: skip
    chat = await _outcome(
        db_session, tenant.id, account_id=account.id, platform="discord", channel="c1",
        thread="t1", cost=Decimal("0.5"), at=NOW - timedelta(hours=1),
    )  # fmt: skip
    await _outcome(
        db_session, tenant.id, account_id=account.id, platform="discord", channel="c1",
        thread="t2", cost=None, at=NOW - timedelta(hours=2),
    )  # fmt: skip
    await _outcome(
        db_session, tenant.id, account_id=other.id, platform="discord", channel="c1",
        thread="t3", cost=Decimal("9"), at=NOW - timedelta(hours=2),
    )  # fmt: skip

    for n, (channel, session) in enumerate(
        [
            ("c1", None),
            ("c1", None),
            (f"web:{thread.id}", None),
            (None, None),
            ("c2", "routine-run"),
        ]
    ):
        await _event(
            db_session, tenant, user="ann", agent="ag_a", at=NOW - timedelta(hours=1),
            n=100 + n, channel=channel, session=session,
        )  # fmt: skip
    await _outcome(
        db_session, tenant.id, account_id=None, platform="discord", channel="c2", thread=None,
        cost=None, at=NOW - timedelta(hours=1), origin="routine", session="routine-run",
    )  # fmt: skip

    channels = await views.spend_by_channel(
        db_session, tenant_id=tenant.id, markup=MARKUP, since=since
    )
    assert [(r.platform, r.channel_id, r.origin, r.usd, r.turns) for r in channels] == [
        ("discord", "c1", "chat", Decimal("20.000000"), 2),
        ("discord", None, "chat", Decimal("10.000000"), 1),
        ("discord", "c2", "routine", Decimal("10.000000"), 1),
        ("web", f"web:{thread.id}", "chat", Decimal("10.000000"), 1),
    ], "priced from the usage rows; no channel is a row; a routine's session marks routine spend"
    total = await usage_events.cost_for_tenant_since(db_session, tenant_id=tenant.id, since=since)
    assert sum(r.usd for r in channels) == views.billed(total, MARKUP), (
        "the channels add up to the tenant total"
    )
    names = await views.channel_names(db_session, tenant_id=tenant.id, rows=channels)
    assert names == {("discord", "c1"): "general"}, "web has no channel name"

    turns = await views.recent_turns(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        markup=MARKUP,
        since=since,
        limit=10,
    )
    assert [(t.id, t.title, t.usd) for t in turns] == [
        (chat, "general", Decimal("1.000000")),
        (web, "Quarterly plan", Decimal("3.000000")),
    ], "newest first, own turns only, unpriced turns left out"
    one = await views.recent_turns(
        db_session, tenant_id=tenant.id, account_id=account.id, markup=MARKUP, since=since, limit=1
    )
    assert [t.id for t in one] == [chat], "the limit keeps the newest"


async def test_overview_reports_balance_and_priced_spend_by_day(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await make_ledger_entry(
        db_session, tenant=tenant, delta_usd=Decimal("50"), reason="topup", occurred_at=NOW
    )
    await _event(db_session, tenant, user="ann", agent="ag_a", at=NOW - timedelta(days=1), n=1)
    await _event(db_session, tenant, user=None, agent="ag_a", at=NOW - timedelta(days=1), n=2)
    await _event(db_session, tenant, user="ann", agent="ag_a", at=NOW - timedelta(days=20), n=3)
    await _event(db_session, tenant, user="ann", agent="ag_a", at=NOW - timedelta(days=40), n=4)

    overview = await views.spend_overview(db_session, tenant_id=tenant.id, now=NOW, markup=MARKUP)

    assert overview.spent_usd == Decimal("30.000000") and overview.turns == 3
    assert overview.month_usd == Decimal("20.000000"), "only this month's two calls"
    assert len(overview.daily) == 30
    assert overview.daily[-2].usd == Decimal("20.000000"), "yesterday's two calls, no debit needed"
    assert sum(d.usd for d in overview.daily) == overview.spent_usd, "the chart adds up"
    assert overview.balance_usd == Decimal("50")


async def test_daily_spend_matches_what_the_ledger_debited(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A turn's priced usage times the markup is what its debit takes, so the chart can read
    usage and still agree with the ledger wherever there is one."""
    tenant = await make_tenant(db_session)
    now = datetime.now(UTC)
    for n, (reason, tokens) in enumerate(
        [("turn_debit", 120_000), ("checkpoint_debit", 3_000), ("turn_debit", 70)]
    ):
        event = BetaManagedAgentsSpanModelRequestEndEvent(
            id=f"evt_{n}",
            is_error=False,
            model_request_start_id="start_1",
            model_usage=ma_model_usage(input_tokens=tokens, output_tokens=tokens // 3),
            processed_at=now,
            type="span.model_request_end",
        )
        await record_turn_usage(
            sessionmaker=db_session_factory,
            tenant_id=tenant.id,
            platform_user_id="ann",
            managed_session_id=f"s{n}",
            model_id="claude-opus-4-7",
            event=event,
            markup=MARKUP,
            pricing=MODEL_PRICING.get("claude-opus-4-7"),
            reason=reason,  # pyright: ignore[reportArgumentType]
        )

    overview = await views.spend_overview(db_session, tenant_id=tenant.id, now=now, markup=MARKUP)
    debited = await tenant_ledger.get_spend_by_interval(
        db_session, tenant_id=tenant.id, bounds=views.day_bounds(now), reasons=SPEND_LEDGER_REASONS
    )

    assert overview.spent_usd > 0, "the calls were priced"
    assert sum(d.usd for d in overview.daily) == sum(debited), "usage times markup is the debit"


async def test_spend_in_the_last_thirty_days_reaches_into_the_previous_month(
    db_session: AsyncSession,
) -> None:
    early = datetime(2026, 11, 2, 9, tzinfo=UTC)
    tenant = await make_tenant(db_session)
    await _event(db_session, tenant, user="ann", agent="ag_a", at=early - timedelta(days=10), n=1)
    await _event(db_session, tenant, user="ann", agent="ag_a", at=early - timedelta(hours=1), n=2)
    recent, _ = views.period_window("30d", now=early)
    this_month, _ = views.period_window("this", now=early)

    in_30 = await usage_events.cost_for_tenant_since(db_session, tenant_id=tenant.id, since=recent)
    in_month = await usage_events.cost_for_tenant_since(
        db_session, tenant_id=tenant.id, since=this_month
    )
    overview = await views.spend_overview(db_session, tenant_id=tenant.id, now=early, markup=MARKUP)

    assert (in_30, in_month) == (10.0, 5.0), "10 days ago is in the last 30 days, not this month"
    assert (overview.spent_usd, overview.month_usd) == (Decimal("20.000000"), Decimal("10.000000"))


async def test_breakdowns_add_up_to_the_tenant_total_for_any_window(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    at = NOW - timedelta(days=3)
    for n, (user, agent, channel) in enumerate(
        [("ann", "ag_a", "c1"), ("bob", None, "c1"), (None, "ag_a", None), ("ann", None, None)]
    ):
        await _event(
            db_session, tenant, user=user, agent=agent, at=at, n=n, channel=channel,
        )  # fmt: skip
    await _event(db_session, tenant, user="ann", agent="ag_a", at=NOW - timedelta(days=45), n=9)
    for period in ("30d", "this", "last"):
        since, until = views.period_window(period, now=NOW)  # pyright: ignore[reportArgumentType]
        total = views.billed(
            await usage_events.cost_for_tenant_since(
                db_session, tenant_id=tenant.id, since=since, until=until
            ),
            MARKUP,
        )
        kwargs = {"tenant_id": tenant.id, "markup": MARKUP, "since": since, "until": until}
        people = await views.spend_by_person(db_session, **kwargs)
        agents = await views.spend_by_agent_in_tenant(db_session, **kwargs)
        channels = await views.spend_by_channel(db_session, **kwargs)
        for name, rows in (("people", people), ("agents", agents), ("channels", channels)):
            assert sum(r.usd for r in rows) == total, f"{name} add up to the total for {period}"
        if period == "30d":
            assert total == Decimal("40.000000")
            assert None in {r.user_id for r in people}, "spend with no person is a row"
            assert None in {r.channel_id for r in channels}, "spend with no channel is a row"


async def test_ledger_history_leaves_out_spend_and_files_each_row(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    for key, reason, delta, days in (
        ("a", "topup", "25", 3),
        ("b", "promo_credit", "5", 2),
        ("c", "turn_debit", "-1", 1),
    ):
        await make_ledger_entry(
            db_session,
            tenant=tenant,
            delta_usd=Decimal(delta),
            reason=reason,
            idempotency_key=key,
            occurred_at=NOW - timedelta(days=days),
        )

    lines = await views.ledger_history(db_session, tenant_id=tenant.id, limit=10)

    assert [(x.kind, x.usd) for x in lines] == [
        ("promo", Decimal("5")),
        ("topup", Decimal("25")),
    ], "newest first and no model spend"
    assert [
        x.reason for x in await views.ledger_history(db_session, tenant_id=tenant.id, limit=1)
    ] == ["promo_credit"]


async def test_people_are_named_by_web_person_linked_account_or_platform(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    member_account = await make_account(db_session, tenant=tenant)
    member = await make_web_person(
        db_session, tenant=tenant, account=member_account, display_name="Mia"
    )
    await make_platform_principal(
        db_session, platform="discord", external_id="d-mia", tenant=tenant, account=member_account
    )
    other = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="slack", external_id="u-sam", tenant=tenant, account=other
    )
    await platform_names.upsert_user_names(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        names={"u-sam": KnownName(display_name="Sam")},
    )

    labels = await views.label_people(
        db_session,
        tenant_id=tenant.id,
        user_ids=[str(member.id), "d-mia", "u-sam", "nobody"],
    )

    assert labels[str(member.id)] == views.PersonLabel("Mia", False, member.id)
    assert labels["d-mia"].person_id == member.id, "a linked platform id names the web person"
    assert labels["u-sam"] == views.PersonLabel("Sam", False, None)
    assert "nobody" not in labels, "an id nothing names is left out"

    by_account = await views.web_people_by_account(
        db_session, tenant_id=tenant.id, account_ids=[member_account.id, other.id]
    )
    assert list(by_account) == [member_account.id]


async def test_channel_names_skips_a_web_place_recorded_under_a_chat_platform(
    db_session: AsyncSession,
) -> None:
    """A linked member's web chat bills under their chat platform with a `web:` channel."""
    tenant = await make_tenant(db_session)
    rows = [
        views.ChannelSpend("discord", "web:abc", "turn", Decimal(1), 1),
        views.ChannelSpend("discord", "c1", "turn", Decimal(1), 1),
    ]
    await platform_names.upsert_channel_names(
        db_session, tenant_id=tenant.id, platform="discord", names={"c1": "general"}
    )
    names = await views.channel_names(db_session, tenant_id=tenant.id, rows=rows)
    assert names == {("discord", "c1"): "general"}, "the web place is looked up as no channel"
    assert views.is_web_spend("discord", "web:abc"), "a web place is web spend on any platform"
    assert not views.is_web_spend("discord", "c1"), "a real channel is not"
