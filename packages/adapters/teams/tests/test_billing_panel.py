"""The `billing` panel, driven through the real SDK route: member and admin views, top-ups.

Only the outbound Bot Framework transport and the MCP checkout hop are faked.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from daimon.adapters.teams.billing_panel import (
    ADMIN_ONLY,
    ENTER_CODE,
    NOT_CONFIGURED,
    REDEEM_ADMIN_ONLY,
    UNKNOWN_AMOUNT,
    panel_card,
    plain_name,
    roster_names,
)
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.adapters.teams.identity import DENIED
from daimon.core.billing_panel import BillingPanelState, MemberRow
from daimon.core.channel_budget import ChannelBudgetStatus
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.promo_codes import build_promo_code_terms, hash_promo_code, normalize_promo_code
from daimon.core.promo_credit import ActiveTimedCredit
from daimon.core.stores import promo_codes as promo_store
from daimon.core.stores.domain import ChannelBudgetRow
from daimon.core.stores.teams_installations import record_teams_installation
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_usage_event
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    TeamsApiFake,
    build_teams_runtime,
    make_card_action,
    make_message_activity,
    post_activity,
    running_service,
    teams_settings,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
CHECKOUT_URL = "https://checkout.example/abc"


def _running(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    mcp: httpx.MockTransport | None = None,
) -> AbstractAsyncContextManager[TeamsHttpService]:
    """An admin (AAD_OBJECT_ID) and a member (OTHER_AAD_OBJECT_ID); `mcp` fakes checkout."""
    settings = teams_settings(admins=(AAD_OBJECT_ID,))
    client = None if mcp is None else httpx.AsyncClient(transport=mcp)
    runtime = build_teams_runtime(db_factory, teams=settings, http_client=client)
    runtime.settings.mcp.app_root_url = "https://mcp.example"
    runtime.settings.mcp.jwt_secret = SecretStr("test-jwt-secret-at-least-32-chars-long!!")
    return running_service(runtime, fake)


async def _command(service: TeamsHttpService, fake: TeamsApiFake, user: str) -> str:
    """Send `billing` as `user` and return the card it replies with."""
    seen = len(fake.activity_requests)
    activity = make_message_activity(text="billing", activity_id=f"a-{seen}", aad_object_id=user)
    await post_activity(service, activity)
    async with asyncio.timeout(10):
        while len(fake.activity_requests) == seen:
            await asyncio.sleep(0.01)
    return json.dumps(fake.activity_requests[-1].body, ensure_ascii=False)


def _click(op: str, *, user: str = AAD_OBJECT_ID, **extra: str) -> dict[str, object]:
    return make_card_action("billing", op, user=user, **extra)


async def test_only_the_admin_view_offers_top_ups(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        member = await _command(service, teams_api_fake, OTHER_AAD_OBJECT_ID)
        admin = await _command(service, teams_api_fake, AAD_OBJECT_ID)

    assert "Ask an admin to add credit." in member and "topup" not in member, "members only look"
    assert admin.count('"op": "topup"') == 4, "an admin gets one button per amount"
    assert "admin view" in admin


async def test_top_up_clicks_recheck_admin_and_the_amount(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    posts: list[httpx.Request] = []
    mcp = httpx.MockTransport(lambda r: posts.append(r) or httpx.Response(500))
    async with _running(db_session_factory, teams_api_fake, mcp) as service:
        member = await post_activity(
            service, _click("topup", user=OTHER_AAD_OBJECT_ID, amount="25")
        )
        odd = await post_activity(service, _click("topup", amount="7"))
        stranger = _click("topup", amount="25")
        stranger["conversation"] = {"id": CONVERSATION_ID, "tenantId": str(uuid.UUID(int=99))}
        denied = await post_activity(service, stranger)

    assert member["value"] == ADMIN_ONLY, "a forwarded admin card does nothing for a member"
    assert odd["value"] == UNKNOWN_AMOUNT, "only the offered amounts are accepted"
    assert denied["value"] == DENIED, "an unverified clicker sees nothing"
    assert posts == [], "no checkout was created"


async def test_an_admin_top_up_links_to_checkout(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    posts: list[httpx.Request] = []

    def checkout(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(200, json={"url": CHECKOUT_URL})

    async with _running(db_session_factory, teams_api_fake, httpx.MockTransport(checkout)) as svc:
        response = await post_activity(svc, _click("topup", amount="25"))

    [post] = posts
    assert str(post.url) == "https://mcp.example/billing/checkout"
    assert json.loads(post.content) == {"amount": 25}
    card = json.dumps(response)
    assert "Action.OpenUrl" in card and CHECKOUT_URL in card, "payment opens as a link"


async def test_a_top_up_without_payments_says_so(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        response = await post_activity(service, _click("topup", amount="10"))

    assert NOT_CONFIGURED in json.dumps(response), "no billing routes mounted is not an error"


async def _promo(db_factory: async_sessionmaker[AsyncSession], code: str) -> None:
    async with db_factory.begin() as session:
        await promo_store.insert_promo_code(
            session,
            code_hash=hash_promo_code(normalize_promo_code(code)),
            terms=build_promo_code_terms(amount_usd=Decimal("10"), timed=False),
        )


async def test_an_admin_redeems_a_promo_code_from_the_card(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    await _promo(db_session_factory, "SPRING-2026")
    async with _running(db_session_factory, teams_api_fake) as service:
        member = await _command(service, teams_api_fake, OTHER_AAD_OBJECT_ID)
        admin = await _command(service, teams_api_fake, AAD_OBJECT_ID)
        forwarded = await post_activity(
            service, _click("redeem", user=OTHER_AAD_OBJECT_ID, code="SPRING-2026")
        )
        empty = await post_activity(service, _click("redeem", code=" "))
        wrong = await post_activity(service, _click("redeem", code="WRONG-CODE"))
        redeemed = await post_activity(service, _click("redeem", code="spring-2026"))

    assert '"op": "redeem"' in admin and '"op": "redeem"' not in member, "admins only"
    assert forwarded["value"] == REDEEM_ADMIN_ONLY and empty["value"] == ENTER_CODE
    assert isinstance(wrong["value"], str) and wrong["value"], "a refusal is said, the card kept"
    card = json.dumps(redeemed, ensure_ascii=False)
    assert "Redeemed **$10.00** of credit" in card and "admin view" in card


def _budget_status(channel_id: str, spent: str) -> ChannelBudgetStatus:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    budget = ChannelBudgetRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        platform="teams",
        channel_id=channel_id,
        limit_usd=Decimal("10"),
        window="monthly",
        starts_at=None,
        ends_at=None,
        set_by_account_id=None,
        created_at=now,
        updated_at=now,
    )
    return ChannelBudgetStatus(budget=budget, spent_usd=Decimal(spent), is_active=True)


def test_the_admin_card_lists_channel_budgets_and_the_member_card_does_not() -> None:
    since = datetime(2026, 1, 1, tzinfo=UTC)
    budgets = tuple(_budget_status(f"19:c{i}", str(9 - i)) for i in range(7))
    for is_admin in (True, False):
        state = BillingPanelState(
            is_admin=is_admin,
            caller_user_id="u",
            caller_spend=0.0,
            caller_turns=0,
            caller_cap=None,
            guild_balance_usd=Decimal("1"),
            guild_spend=0.0,
            guild_turns=0,
            guild_distinct_members=0,
            member_rows=(),
            over_cap_count=0,
            channel_budgets=budgets,
        )
        text = json.dumps(
            panel_card(state, since=since).model_dump(by_alias=True), ensure_ascii=False
        )
        if is_admin:
            assert "Channel `19:c0`: $9.00 of $10.00 (monthly) · 90% used" in text
            assert "19:c4" in text and "19:c5" not in text, "only the five most used"
            assert "2 more channel budgets" in text
        else:
            assert "Channel budgets" not in text, "a member sees no other channel"


# ---------------------------------------------------------------------------
# Top spender names and the credit layout
# ---------------------------------------------------------------------------

TEAM_A = "19:team-a@thread.tacv2"
TEAM_B = "19:team-b@thread.tacv2"


async def test_the_admin_card_names_top_spenders_from_team_rosters(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    gone = str(uuid.UUID(int=7))
    tenant_id = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
    async with db_session_factory.begin() as session:
        await record_teams_installation(
            session, tenant_id=tenant_id, team_id=TEAM_A, group_id="g", name="A"
        )
        tenant = await get_tenant(session, tenant_id)
        assert tenant is not None
        for user, tokens in ((OTHER_AAD_OBJECT_ID, 2000), (gone, 1000)):
            await make_usage_event(
                session, tenant=tenant, platform_user_id=user, input_tokens=tokens
            )
    teams_api_fake.names[OTHER_AAD_OBJECT_ID] = "Maya *Chen*"
    teams_api_fake.absent.add(gone)

    async with _running(db_session_factory, teams_api_fake) as service:
        admin = await _command(service, teams_api_fake, AAD_OBJECT_ID)

    assert "1. Maya \\\\*Chen\\\\*  $" in admin, "a roster name, its markdown escaped"
    assert "2. User 0007  $" in admin, "someone no roster has keeps `User XXXX`"
    assert "<at>" not in admin and '"mention"' not in admin, "a name is never a mention"
    looked_up = sorted(
        r.url.rsplit("/conversations/", 1)[-1]
        for r in teams_api_fake.requests
        if r.url.endswith((OTHER_AAD_OBJECT_ID, gone))
    )
    assert looked_up == [f"{TEAM_A}/members/{OTHER_AAD_OBJECT_ID}", f"{TEAM_A}/members/{gone}"], (
        "each shown spender is looked up once, on the installed team's roster"
    )


async def test_roster_names_tries_each_team_and_drops_the_unresolved() -> None:
    rosters = {TEAM_A: {"u1": "Ann"}, TEAM_B: {"u2": "Bo"}}

    async def roster_name(team_id: str, user_id: str) -> str | None:
        if user_id == "u4":
            raise httpx.HTTPStatusError(
                "gone", request=httpx.Request("GET", "x"), response=httpx.Response(404)
            )
        return rosters[team_id].get(user_id)

    names = await roster_names(
        roster_name, team_ids=[TEAM_A, TEAM_B], user_ids=["u1", "u2", "u3", "u4"]
    )

    assert names == {"u1": "Ann", "u2": "Bo"}, "the first roster with a name wins"


async def test_roster_names_gives_up_on_slow_lookups_after_the_timeout() -> None:
    async def roster_name(team_id: str, user_id: str) -> str | None:
        if user_id == "slow":
            await asyncio.sleep(10)
        return f"name-{user_id}"

    async with asyncio.timeout(2):
        names = await roster_names(
            roster_name, team_ids=[TEAM_A], user_ids=["fast", "slow"], timeout_s=0.1
        )

    assert names == {"fast": "name-fast"}, "a lookup pending at the timeout is left out"


async def test_roster_names_without_an_installed_team_looks_nobody_up() -> None:
    async def roster_name(team_id: str, user_id: str) -> str | None:
        raise AssertionError("no lookup without a team")

    assert await roster_names(roster_name, team_ids=[], user_ids=["u1"]) == {}


def test_plain_name_shows_markdown_and_tags_literally() -> None:
    assert plain_name("<at>@everyone</at> **x** [a](b)\nnext") == (
        "at@everyone/at \\*\\*x\\*\\* \\[a\\](b) next"
    )


def _card_text(state: BillingPanelState, *, now: datetime | None = None) -> list[str]:
    card = panel_card(state, since=datetime(2026, 1, 1, tzinfo=UTC), now=now)
    return [str(e.get("text", "")) for e in card.model_dump(by_alias=True)["body"]]


def _state(**overrides: object) -> BillingPanelState:
    base = BillingPanelState(
        is_admin=True,
        caller_user_id="u",
        caller_spend=0.0,
        caller_turns=0,
        caller_cap=None,
        guild_balance_usd=Decimal("62.4"),
        guild_spend=0.0,
        guild_turns=0,
        guild_distinct_members=0,
        member_rows=(MemberRow("u", "User 0000", 1.0, 1, True),),
        over_cap_count=0,
    )
    return dataclasses.replace(base, **overrides)  # pyright: ignore[reportArgumentType]


def test_the_credit_without_timed_credit_is_just_the_total() -> None:
    lines = _card_text(_state())
    at = lines.index("🏦 **Organisation credit**")
    assert lines[at + 1] == "**$62.40** total credit left"
    assert lines[at + 2] == "🏆 **Top spenders**", "nothing between the total and the next group"


def test_the_credit_says_how_much_of_it_expires_in_one_grey_line() -> None:
    credits = tuple(
        ActiveTimedCredit(Decimal(n), datetime(2026, 1, 10 + n, tzinfo=UTC)) for n in range(1, 5)
    )
    card = panel_card(_state(timed_credit=credits), since=datetime(2026, 1, 1, tzinfo=UTC))
    body = card.model_dump(by_alias=True, exclude_none=True)["body"]
    at = next(i for i, e in enumerate(body) if e.get("text") == "**$62.40** total credit left")
    note = body[at + 1]
    assert note["text"] == (
        "Includes $10.00 that expires, first on {{DATE(2026-01-11T00:00:00Z, SHORT)}} "
        "{{TIME(2026-01-11T00:00:00Z)}}. It's used first."
    )
    assert note.get("isSubtle") and note.get("size") == "Small"
    assert body[at + 2]["text"] == "🏆 **Top spenders**", "one line, however many credits"


def test_a_negative_balance_says_no_credit_left_and_still_shows_timed_credit() -> None:
    credit = (ActiveTimedCredit(Decimal("5"), datetime(2026, 1, 20, tzinfo=UTC)),)
    lines = _card_text(_state(is_admin=False, guild_balance_usd=Decimal("-3"), timed_credit=credit))
    assert "**No credit left** · $3.00 spent beyond it" in lines
    assert any(line.startswith("Includes $5.00 that expires ") for line in lines)
    assert lines[-1] == "Ask an admin to add credit."


def test_the_header_own_use_and_spender_rows_are_short() -> None:
    admin = _card_text(_state(guild_spend=48.17, guild_distinct_members=9))
    assert admin[1] == "January 2026 · $48.17 spent by 9 people"
    assert "1. User 0000 (you)  $1.00" in admin
    member = _card_text(_state(is_admin=False, caller_spend=11.5, caller_cap=Decimal("25")))
    assert member[1:3] == ["January 2026", "**You**: $11.50 of your $25.00 this month"]


def test_the_channel_budget_group_sits_between_credit_and_top_spenders() -> None:
    status = _budget_status("19:c", "1.2")
    lines = _card_text(_state(channel_budget=status, channel_budgets=(status,)))
    order = [
        lines.index(group)
        for group in (
            "🏦 **Organisation credit**",
            "📊 **Channel budget**",
            "🏆 **Top spenders**",
            "📊 **Channel budgets**",
        )
    ]
    assert order == sorted(order), order
    assert lines[order[1] + 1] == "$1.20 of $10.00 spent this month"
