"""The chat billing panels' shared figures: formatters, the snapshot read and the checkout hop."""

from __future__ import annotations

import functools
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import jwt as pyjwt
import pytest
from daimon.core.billing_panel import (
    BillingPanelState,
    admin_summary,
    caller_line,
    channel_budget_phrase,
    create_checkout,
    credit_headline,
    estimate_turns,
    expiry_rows,
    fmt_usd,
    load_billing_snapshot,
    lookup_line,
    panel_tone,
    redeemed_text,
    spender_line,
    timed_credit_note,
    turns_phrase,
)
from daimon.core.channel_budget import ChannelBudgetStatus
from daimon.core.errors import DaimonError
from daimon.core.promo_codes import build_promo_code_terms
from daimon.core.promo_credit import ActiveTimedCredit, PromoRedeemed
from daimon.core.stores import promo_codes as promo_store
from daimon.core.stores import tenants, usage_events
from daimon.core.stores.domain import BudgetWindow, ChannelBudgetRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.platform_names import KnownName, upsert_user_names
from daimon.testing.factories import (
    make_account,
    make_channel_budget,
    make_ledger_entry,
    make_tenant,
)
from mux.testing.drivers.anthropic.ma_models import ma_model_usage
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_CALLER_ID = "U_CALLER"
_OTHER_ID = "U_OTHER"
_SINCE = datetime(2025, 1, 1, tzinfo=UTC)
_NOW = datetime(2025, 1, 15, tzinfo=UTC)
_CHECKOUT_URL = "https://checkout.example/abc"


async def _tenant_with_usage(session: AsyncSession) -> uuid.UUID:
    """A tenant where the other user out-spends the caller."""
    tenant = await make_tenant(session, platform="slack", workspace_id="T_BILLING_TEST")
    for user_id, tokens in ((_CALLER_ID, 1000), (_OTHER_ID, 10_000)):
        await usage_events.record(
            session,
            tenant_id=tenant.id,
            platform_user_id=user_id,
            managed_session_id=f"sess-{user_id}",
            model="claude-opus-4-7",
            model_usage=ma_model_usage(input_tokens=tokens, output_tokens=tokens),
            event_id=f"evt-{user_id}",
        )
    await session.commit()
    return tenant.id


def test_fmt_usd_formats_cents_and_thousands() -> None:
    assert fmt_usd(12.5) == "$12.50", "two decimal places"
    assert fmt_usd(0.0) == "$0.00", "zero still shows cents"
    assert fmt_usd(Decimal("1000")) == "$1,000.00", "Decimal gets a thousands separator"
    assert fmt_usd(2500.75) == "$2,500.75", "float gets a thousands separator"
    assert fmt_usd(Decimal("-12.5")) == "-$12.50", "the sign goes before the dollar"


def _ends(day: int) -> datetime:
    return datetime(2025, 1, day, tzinfo=UTC)


def test_credit_headline_is_the_figure_and_the_words_under_it() -> None:
    assert credit_headline(Decimal("62.4")) == ("$62.40", "total credit left")
    assert credit_headline(Decimal("0")) == ("No credit left", None)
    assert credit_headline(Decimal("-3.1")) == ("No credit left", "$3.10 spent beyond it")


def test_timed_credit_note_sums_what_expires_without_dates() -> None:
    assert timed_credit_note(()) is None
    credits = [ActiveTimedCredit(Decimal(n), _ends(20 + n)) for n in (4, 1, 2, 3)]
    assert timed_credit_note(credits) == "Includes $10.00 that expires. It's used first."


def test_expiry_rows_list_five_soonest_first_and_count_the_rest() -> None:
    credits = [ActiveTimedCredit(Decimal(n), _ends(10 + n)) for n in (7, 1, 2, 3, 4, 5, 6)]
    rows = expiry_rows(credits, when=lambda moment: f"{moment:%b} {moment.day}")
    assert rows == [
        "$1.00 on Jan 11",
        "$2.00 on Jan 12",
        "$3.00 on Jan 13",
        "$4.00 on Jan 14",
        "$5.00 on Jan 15",
        "+ 2 more",
    ]


def test_panel_tone_flags_no_credit_over_cap_and_soon_expiring_credit() -> None:
    soon = [ActiveTimedCredit(Decimal("5"), _NOW + timedelta(days=3))]
    later = [ActiveTimedCredit(Decimal("5"), _NOW + timedelta(days=30))]
    tone = functools.partial(panel_tone, caller_spend=1.0, caller_cap=None, now=_NOW)
    assert tone(balance=Decimal("0"), credits=()) == "alert", "no credit left"
    assert tone(balance=Decimal("9"), credits=soon) == "warning", "expires within a week"
    assert tone(balance=Decimal("9"), credits=later) is None
    over = panel_tone(
        balance=Decimal("9"), caller_spend=6.0, caller_cap=Decimal("5"), credits=(), now=_NOW
    )
    assert over == "alert", "the caller is over their cap"


def test_the_month_summary_and_own_use_lines() -> None:
    since = datetime(2026, 10, 1, tzinfo=UTC)
    assert admin_summary(since, spend=48.17, people=9) == (
        "October 2026",
        "$48.17 spent by 9 people",
    ), "the month and the spend are two lines, no separator"
    assert admin_summary(since, spend=1.0, people=1) == ("October 2026", "$1.00 spent by 1 person")
    assert admin_summary(since, spend=0.0, people=0) == ("October 2026", "Nothing used yet")
    assert turns_phrase(1250) == "about 1,250 answers"
    assert caller_line(11.5, Decimal("25"), 71) == "$11.50 of your $25.00 this month"
    assert caller_line(11.5, None, 71) == "$11.50 used this month"
    assert caller_line(0.0, Decimal("25"), 0) == "Nothing used this month"
    assert lookup_line(14.02, 88) == "$14.02 this month"
    assert lookup_line(0.0, 0) == "Nothing used this month"
    assert spender_line(2, "derwells", cost=11.5, is_caller=True) == "2. derwells (you)  $11.50"


def _budget(
    window: BudgetWindow,
    *,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    spent: str = "1.2",
    is_active: bool = True,
) -> ChannelBudgetStatus:
    row = ChannelBudgetRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        platform="slack",
        channel_id="C1",
        limit_usd=Decimal("5"),
        window=window,
        starts_at=starts_at,
        ends_at=ends_at,
        set_by_account_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    return ChannelBudgetStatus(budget=row, spent_usd=Decimal(spent), is_active=is_active)


@pytest.mark.parametrize(
    ("status", "phrase"),
    [
        (_budget("monthly"), "$1.20 of $5.00 used this month"),
        (_budget("total"), "$1.20 of $5.00 used in total"),
        (_budget("total", starts_at=_ends(2)), "$1.20 of $5.00 used since 2025-01-02"),
        (
            _budget("total", starts_at=_ends(20), spent="0", is_active=False),
            "$5.00 budget from 2025-01-20",
        ),
        (
            _budget("fixed", starts_at=_ends(2), ends_at=_ends(20)),
            "$1.20 of $5.00 used from 2025-01-02 until 2025-01-20",
        ),
        (
            _budget("fixed", starts_at=_ends(20), ends_at=_ends(25), spent="0", is_active=False),
            "$5.00 budget from 2025-01-20 until 2025-01-25",
        ),
        (
            _budget("fixed", starts_at=_ends(2), ends_at=_ends(10), is_active=False),
            "$1.20 of $5.00 used from 2025-01-02 until 2025-01-10 (ended)",
        ),
    ],
)
def test_channel_budget_phrase_follows_the_window(status: ChannelBudgetStatus, phrase: str) -> None:
    assert channel_budget_phrase(status, now=_NOW) == phrase


def test_estimate_turns_uses_the_tenant_average_or_the_fallback() -> None:
    assert estimate_turns(10.0, guild_spend=0.0, guild_turns=0) == 100, "$0.10/turn fallback"
    assert estimate_turns(10.0, guild_spend=20.0, guild_turns=4) == 2, "$5/turn average"
    assert estimate_turns(10.0, guild_spend=0.0, guild_turns=5) == 100, (
        "zero spend with turns falls back rather than dividing by zero cost"
    )


async def test_load_billing_snapshot_member_reads_only_caller_data(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant_with_usage(db_session)
    state = await load_billing_snapshot(
        db_session,
        tenant_id=tenant_id,
        platform_user_id=_CALLER_ID,
        is_admin=False,
        since=_SINCE,
        now=_NOW,
    )

    assert state.is_admin is False, "a member gets the member snapshot"
    assert state.caller_spend > 0.0 and state.caller_turns == 1, "the caller's own usage"
    assert state.member_rows == (), "no per-member breakdown for a member"
    assert (state.guild_spend, state.guild_turns, state.guild_distinct_members) == (0.0, 0, 0), (
        "tenant totals are not read for a member"
    )


async def test_load_billing_snapshot_admin_sorts_members_by_spend(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant_with_usage(db_session)
    state = await load_billing_snapshot(
        db_session,
        tenant_id=tenant_id,
        platform_user_id=_CALLER_ID,
        is_admin=True,
        since=_SINCE,
        now=_NOW,
    )

    assert state.is_admin is True, "an admin gets the admin snapshot"
    assert [row.platform_user_id for row in state.member_rows] == [_OTHER_ID, _CALLER_ID], (
        "rows are ordered by spend, highest first"
    )
    assert state.member_rows[1].is_caller, "the caller's own row is flagged"
    assert state.guild_distinct_members == 2, "both spenders are counted"
    assert [row.display_name for row in state.member_rows] == [None, None], (
        "without a platform no stored name is read, and no `User 1234` stands in"
    )


async def test_load_billing_snapshot_names_rows_from_the_stored_names(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant_with_usage(db_session)
    for platform, user_id in (("slack", _OTHER_ID), ("discord", _CALLER_ID)):
        await get_or_create_platform_principal(
            db_session, tenant_id=tenant_id, platform=platform, external_id=user_id
        )
    await upsert_user_names(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        names={_OTHER_ID: KnownName(display_name="Maya Chen", handle="maya")},
    )
    await upsert_user_names(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        names={_CALLER_ID: KnownName(display_name="Wrong platform")},
    )

    state = await load_billing_snapshot(
        db_session,
        tenant_id=tenant_id,
        platform_user_id=_CALLER_ID,
        is_admin=True,
        since=_SINCE,
        now=_NOW,
        platform="slack",
    )

    assert [row.display_name for row in state.member_rows] == ["Maya Chen", None], (
        "a stored name for this platform names the row; another platform's does not"
    )


async def test_load_billing_snapshot_admin_caps_at_25_members(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_BILLING_MANY")
    for i in range(30):
        await usage_events.record(
            db_session,
            tenant_id=tenant.id,
            platform_user_id=f"U_MANY_{i:03d}",
            managed_session_id=f"sess-many-{i}",
            model="claude-opus-4-7",
            model_usage=ma_model_usage(input_tokens=100 * (i + 1), output_tokens=50 * (i + 1)),
            event_id=f"evt-many-{i}",
        )
    await db_session.commit()

    state = await load_billing_snapshot(
        db_session,
        tenant_id=tenant.id,
        platform_user_id="U_MANY_000",
        is_admin=True,
        since=_SINCE,
        now=_NOW,
    )

    assert len(state.member_rows) == 25, "member rows are capped at 25"
    assert state.over_cap_count == 5, "the rows past the cap are counted"


async def test_load_billing_snapshot_empty_period_is_well_formed(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_BILLING_EMPTY")
    await db_session.commit()
    state = await load_billing_snapshot(
        db_session,
        tenant_id=tenant.id,
        platform_user_id="U_EMPTY",
        is_admin=False,
        since=_SINCE,
        now=_NOW,
    )

    assert (state.caller_spend, state.caller_turns, state.member_rows) == (0.0, 0, ()), (
        "an empty month reads as zeros, not an error"
    )


def _mcp_settings() -> MagicMock:
    settings = MagicMock()
    settings.app_root_url = "https://mcp.example.com"
    settings.jwt_secret = SecretStr("test-jwt-secret-at-least-32-chars-long!!")
    return settings


async def test_create_checkout_posts_only_the_amount_with_a_bearer_token(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    account = await make_account(db_session)
    await db_session.commit()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"url": _CHECKOUT_URL})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        url = await create_checkout(
            client,
            session_factory=db_session_factory,
            settings=_mcp_settings(),
            account_id=account.id,
            amount=25,
        )

    assert url == _CHECKOUT_URL, "the Checkout URL from the MCP answer"
    [request] = captured
    assert request.method == "POST" and request.url.path == "/billing/checkout"
    assert json.loads(request.content) == {"amount": 25}, "the tenant never rides in the body"
    assert request.headers["authorization"].startswith("Bearer "), "the hop is authenticated"


async def test_create_checkout_sends_a_plain_account_token(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The checkout route never checks admin, so the hop carries no admin or internal claim."""
    account = await make_account(db_session)
    await db_session.commit()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"url": _CHECKOUT_URL})

    account_id = account.id
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await create_checkout(
            client,
            session_factory=db_session_factory,
            settings=_mcp_settings(),
            account_id=account_id,
            amount=10,
        )

    token = captured[0].headers["authorization"].removeprefix("Bearer ")
    claims = pyjwt.decode(token, options={"verify_signature": False})
    assert claims["sub"] == str(account_id), "the verifier derives the tenant from the account"
    from daimon.core.stores.mcp_tokens import get_mcp_token

    async with db_session_factory() as session:
        row = await get_mcp_token(session, jti=uuid.UUID(claims["jti"]))
    assert row is not None and row.kind == "cli" and row.label == "billing-panel"
    assert claims["exp"] - claims["iat"] == 600
    assert "internal" not in claims and "is_admin" not in claims, (
        "a checkout hop must not mint an internal admin bearer"
    )


async def test_create_checkout_raises_on_non_2xx(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    account = await make_account(db_session)
    await db_session.commit()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "invalid amount"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await create_checkout(
                client,
                session_factory=db_session_factory,
                settings=_mcp_settings(),
                account_id=account.id,
                amount=999,
            )


async def test_create_checkout_refuses_without_mcp_settings(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = MagicMock()
    settings.app_root_url = None
    settings.jwt_secret = None
    async with httpx.AsyncClient() as client:
        with pytest.raises(DaimonError, match="DAIMON_MCP__PUBLIC_URL"):
            await create_checkout(
                client,
                session_factory=db_session_factory,
                settings=settings,
                account_id=uuid.uuid4(),
                amount=10,
            )


async def test_load_billing_snapshot_flags_the_codes_each_viewer_may_redeem(
    db_session: AsyncSession,
) -> None:
    """An admin is flagged for any code; a member only for a channel code."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_PROMO")

    async def snapshot(is_admin: bool) -> bool:
        state = await load_billing_snapshot(
            db_session,
            tenant_id=tenant.id,
            platform_user_id=_CALLER_ID,
            is_admin=is_admin,
            since=_SINCE,
            now=_NOW,
        )
        return state.has_redeemable_promo_code

    assert not await snapshot(True), "no code should mean no redeem button"
    terms = build_promo_code_terms(amount_usd=Decimal("10"), timed=False)
    await promo_store.insert_promo_code(db_session, code_hash="h", terms=terms)
    assert await snapshot(True), "a redeemable code should show the redeem button to admins"
    assert not await snapshot(False), "a server-wide code gives members no redeem button"
    channel = build_promo_code_terms(
        amount_usd=Decimal("10"), timed=False, channel_cap_usd=Decimal("10")
    )
    await promo_store.insert_promo_code(db_session, code_hash="c", terms=channel)
    assert await snapshot(False), "a channel code shows members the redeem button"


async def test_load_billing_snapshot_hides_redemption_when_the_lookup_fails(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed promo lookup hides the button and leaves the rest of the read working."""

    async def failing_lookup(session: AsyncSession, *, now: datetime, **_: object) -> bool:
        await session.execute(text("SELECT 1/0"))  # aborts the surrounding transaction
        return True

    monkeypatch.setattr("daimon.core.billing_panel.has_redeemable_promo_code", failing_lookup)
    tenant_id = await _tenant_with_usage(db_session)

    state = await load_billing_snapshot(
        db_session,
        tenant_id=tenant_id,
        platform_user_id=_CALLER_ID,
        is_admin=True,
        since=_SINCE,
        now=_NOW,
    )

    assert state.is_admin and not state.has_redeemable_promo_code, (
        "a failed lookup should still load the admin panel, without the redeem button"
    )


async def test_load_billing_snapshot_lists_channel_budgets_for_admins_only(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant_with_usage(db_session)
    tenant = await tenants.get_tenant(db_session, tenant_id)
    assert tenant is not None
    for channel, limit, spent in (("low", "10", "1"), ("high", "10", "9"), ("zero", "0", "0")):
        await make_channel_budget(
            db_session, tenant=tenant, channel_id=channel, limit_usd=Decimal(limit)
        )
        if spent != "0":
            await make_ledger_entry(
                db_session,
                tenant=tenant,
                delta_usd=-Decimal(spent),
                channel_id=channel,
                occurred_at=_NOW,
            )
    await make_channel_budget(db_session, tenant=tenant, platform="discord", channel_id="other")
    await db_session.commit()

    async def snapshot(is_admin: bool) -> BillingPanelState:
        return await load_billing_snapshot(
            db_session,
            tenant_id=tenant_id,
            platform_user_id=_CALLER_ID,
            is_admin=is_admin,
            since=_SINCE,
            now=_NOW,
            platform="slack",
        )

    admin = await snapshot(True)
    assert [s.budget.channel_id for s in admin.channel_budgets] == ["zero", "high", "low"], (
        "this platform's budgets, the most used first"
    )
    assert [s.percent_used for s in admin.channel_budgets] == [100, 90, 10], "share of the limit"
    assert (await snapshot(False)).channel_budgets == (), "a member sees no other channel"


def test_redeemed_text_names_what_each_redemption_did() -> None:
    """One wording for every surface; the surface supplies only its markup."""

    def text(**kw: object) -> str:
        base: dict[str, object] = {
            "promo_code_id": uuid.uuid4(),
            "kind": "credit",
            "amount_usd": Decimal("10"),
            "credit_starts_at": None,
            "credit_ends_at": None,
            "granted": True,
            "balance_usd": Decimal("12.5"),
        }
        return redeemed_text(
            PromoRedeemed(**(base | kw)),  # pyright: ignore[reportArgumentType]  # test builds rows from a dict
            bold=lambda s: f"*{s}*",
            when=lambda t: t.strftime("%d %b"),
            channel=lambda c: f"#{c}",
            owner="the server's",
        )

    end = datetime(2026, 5, 20, tzinfo=UTC)
    assert text() == "🎟️ Added *$10.00* of credit.\n\nBalance: *$12.50*"
    assert text(channel_id="c1", channel_limit_usd=Decimal("15")) == (
        "🎟️ Added *$10.00* to #c1's budget.\n\nIts limit is now *$15.00*."
    )
    assert text(channel_id="c1") == (
        "🎟️ Added *$10.00* of credit for #c1.\n\n"
        "It has no spending limit, so it uses the server's credit: *$12.50*."
    )
    assert text(kind="timed", credit_ends_at=end) == (
        "🎟️ Added *$10.00* of credit.\n\nUsed before credit with no expiry. "
        "Anything unused expires 20 May."
    )
    assert text(kind="timed", credit_starts_at=end, credit_ends_at=end, granted=False) == (
        "🎟️ Credit scheduled: *$10.00* from 20 May until 20 May."
    )


async def test_checkout_registration_is_committed_before_the_http_hop(db_engine, db_clean):
    from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory.begin() as session:
        account = await make_account(session)
    secret = _mcp_settings().jwt_secret.get_secret_value().encode()
    verifier = DaimonJWTVerifier(secret=secret, sessionmaker=factory)
    observed = []

    async def checkout(request: httpx.Request) -> httpx.Response:
        token = request.headers["authorization"].removeprefix("Bearer ")
        assert await verifier.verify_token(token) is not None
        observed.append(token)
        return httpx.Response(200, json={"url": _CHECKOUT_URL})

    async with httpx.AsyncClient(transport=httpx.MockTransport(checkout)) as client:
        await create_checkout(
            client,
            settings=_mcp_settings(),
            session_factory=factory,
            account_id=account.id,
            amount=10,
        )
    claims = pyjwt.decode(observed[0], options={"verify_signature": False})
    assert claims["jti"]
    assert await verifier.verify_token(observed[0]) is None


@pytest.mark.parametrize("outcome", ["ok", "refused", "transport-error", "bad-json"])
async def test_checkout_hop_revokes_its_registered_bearer_on_every_exit(
    db_engine, db_clean, outcome
):
    from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory.begin() as session:
        account = await make_account(session)
    secret = _mcp_settings().jwt_secret.get_secret_value().encode()
    verifier = DaimonJWTVerifier(secret=secret, sessionmaker=factory)
    tokens = []

    async def http(request):
        token = request.headers["authorization"].removeprefix("Bearer ")
        assert await verifier.verify_token(token) is not None
        tokens.append(token)
        if outcome == "transport-error":
            raise httpx.ConnectError("offline", request=request)
        if outcome == "bad-json":
            return httpx.Response(200, text="not-json")
        return httpx.Response(403 if outcome == "refused" else 200, json={"url": _CHECKOUT_URL})

    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
        if outcome == "ok":
            assert (
                await create_checkout(
                    client,
                    settings=_mcp_settings(),
                    session_factory=factory,
                    account_id=account.id,
                    amount=10,
                )
                == _CHECKOUT_URL
            )
        else:
            with pytest.raises((httpx.HTTPError, ValueError)):
                await create_checkout(
                    client,
                    settings=_mcp_settings(),
                    session_factory=factory,
                    account_id=account.id,
                    amount=10,
                )
    assert len(tokens) == 1 and await verifier.verify_token(tokens[0]) is None


@pytest.mark.parametrize("outcome", ["ok", "refused", "transport-error", "bad-json"])
@pytest.mark.parametrize("failure_at", ["update", "commit"])
async def test_checkout_revoke_database_failure_preserves_the_real_outcome(
    db_engine, db_clean, outcome, failure_at
):
    from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
    from structlog.testing import capture_logs

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory.begin() as session:
        account = await make_account(session)
    secret = _mcp_settings().jwt_secret.get_secret_value().encode()
    verifier = DaimonJWTVerifier(secret=secret, sessionmaker=factory)
    tokens = []
    original_transport_error = httpx.ConnectError("checkout transport failed")
    response = None

    async def http(request):
        nonlocal response
        token = request.headers["authorization"].removeprefix("Bearer ")
        assert await verifier.verify_token(token) is not None
        tokens.append(token)
        if outcome == "transport-error":
            raise original_transport_error
        response = (
            httpx.Response(200, text="not-json")
            if outcome == "bad-json"
            else httpx.Response(403 if outcome == "refused" else 200, json={"url": _CHECKOUT_URL})
        )
        return response

    # Exercise actual database statement AND transaction-exit failures, not a
    # patched cleanup owner. Both must leave the original HTTP outcome intact.
    deferred = "DEFERRABLE INITIALLY DEFERRED" if failure_at == "commit" else "NOT DEFERRABLE"
    async with db_engine.begin() as connection:
        await connection.execute(
            text("""
            CREATE FUNCTION checkout_revoke_failure() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'cleanup-private-marker'; END;
            $$
        """)
        )
        await connection.execute(
            text(f"""
            CREATE CONSTRAINT TRIGGER checkout_revoke_failure AFTER UPDATE ON mcp_tokens
            {deferred} FOR EACH ROW EXECUTE FUNCTION checkout_revoke_failure()
        """)
        )
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
            with capture_logs() as logs:
                if outcome == "ok":
                    assert (
                        await create_checkout(
                            client,
                            settings=_mcp_settings(),
                            session_factory=factory,
                            account_id=account.id,
                            amount=10,
                        )
                        == _CHECKOUT_URL
                    )
                else:
                    expected = {
                        "refused": httpx.HTTPStatusError,
                        "transport-error": httpx.ConnectError,
                        "bad-json": json.JSONDecodeError,
                    }[outcome]
                    with pytest.raises(expected) as raised:
                        await create_checkout(
                            client,
                            settings=_mcp_settings(),
                            session_factory=factory,
                            account_id=account.id,
                            amount=10,
                        )
                    if outcome == "refused":
                        assert raised.value.response is response
                    elif outcome == "transport-error":
                        assert raised.value is original_transport_error
        failures = [
            event for event in logs if event.get("event") == "billing_panel.checkout_revoke_failed"
        ]
        assert len(failures) == 1 and failures[0]["error_type"]
        assert "cleanup-private-marker" not in repr(logs)
        assert str(account.id) not in repr(logs)
        assert len(tokens) == 1 and tokens[0] not in repr(logs)
        # Failed cleanup does not extend the existing ten-minute upper bound.
        assert await verifier.verify_token(tokens[0]) is not None
        claims = pyjwt.decode(tokens[0], options={"verify_signature": False})
        assert 0 < claims["exp"] - claims["iat"] <= 600
    finally:
        async with db_engine.begin() as connection:
            await connection.execute(text("DROP TRIGGER checkout_revoke_failure ON mcp_tokens"))
            await connection.execute(text("DROP FUNCTION checkout_revoke_failure()"))
