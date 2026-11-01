"""What the credits page reads: pure helpers, then the reads over real Postgres."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from daimon.core import credit_views as views
from daimon.core._models import UsageEvent
from daimon.core.stores import platform_names, turn_outcomes, web_chat
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.platform_names import KnownName
from daimon.core.turn.termination import TerminationReason
from daimon.testing.factories import (
    make_account,
    make_ledger_entry,
    make_platform_principal,
    make_tenant,
    make_usage_event,
    make_web_person,
)
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 3, 16, 12, tzinfo=UTC)
MARKUP = Decimal("2")


def test_billed_applies_the_markup_and_treats_unknown_as_nothing() -> None:
    assert views.billed(0.5, MARKUP) == Decimal("1.000000")
    assert views.billed(None, MARKUP) == Decimal(0), "an unknown cost bills nothing"


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


def test_day_bounds_cover_the_month() -> None:
    bounds = views.day_bounds(NOW)
    assert len(bounds) == 32, "31 days plus the end of March"
    assert bounds[0] == datetime(2026, 3, 1, tzinfo=UTC)
    assert bounds[-1] == datetime(2026, 4, 1, tzinfo=UTC)


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
) -> None:
    """One Opus call (1M input tokens, $5 raw) recorded at `at`; `n` makes it distinct."""
    row = await make_usage_event(
        db_session,
        tenant=tenant,
        platform_user_id=user,
        managed_session_id=f"s{n}",
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
        ("ag_a", Decimal("30.000000")),
        (None, Decimal("10.000000")),
        ("ag_b", Decimal("10.000000")),
    ], "agent-less spend is its own row and NULL-user spend is left out"

    people = await views.spend_by_person(
        db_session, tenant_id=tenant.id, markup=MARKUP, since=start
    )
    assert [(r.user_id, r.usd, r.turns) for r in people] == [
        ("ann", Decimal("30.000000"), 3),
        ("bob", Decimal("20.000000"), 2),
    ]

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
            session_id=f"sess-{outcome_id}",
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

    channels = await views.spend_by_channel(
        db_session, tenant_id=tenant.id, markup=MARKUP, since=since
    )
    assert [(r.platform, r.channel_id, r.usd, r.turns) for r in channels] == [
        ("discord", "c1", Decimal("19.000000"), 3),
        ("web", None, Decimal("3.000000"), 1),
    ], "billed channel spend counts every turn but prices only the known part"
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


async def test_month_overview_reports_balance_spend_and_days(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await make_ledger_entry(
        db_session, tenant=tenant, delta_usd=Decimal("50"), reason="topup", occurred_at=NOW
    )
    await make_ledger_entry(
        db_session,
        tenant=tenant,
        delta_usd=Decimal("-4"),
        reason="turn_debit",
        occurred_at=datetime(2026, 3, 3, 8, tzinfo=UTC),
    )
    await _event(db_session, tenant, user="ann", agent="ag_a", at=NOW - timedelta(days=1), n=1)

    overview = await views.month_overview(db_session, tenant_id=tenant.id, now=NOW, markup=MARKUP)

    assert overview.spent_usd == Decimal("10.000000") and overview.turns == 1
    assert len(overview.daily) == 31
    assert overview.daily[2].usd == Decimal("4"), "the ledger debit lands on its day"
    assert sum(d.usd for d in overview.daily) == Decimal("4")
    assert overview.balance_usd == Decimal("46"), "top-up less the debit"


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
