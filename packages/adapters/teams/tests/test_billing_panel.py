"""The `billing` panel, driven through the real SDK route: member and admin views, top-ups.

Only the outbound Bot Framework transport and the MCP checkout hop are faked.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import uuid
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from daimon.adapters.teams.billing_panel import (
    ADMIN_ONLY,
    ENTER_CODE,
    NOT_CONFIGURED,
    REDEEM_ADMIN_ONLY,
    UNKNOWN_AMOUNT,
    channel_labels,
    panel_card,
    plain_name,
    roster_names,
)
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.adapters.teams.identity import DENIED, TeamsInbound
from daimon.adapters.teams.names import remember_inbound
from daimon.core import platform_names
from daimon.core.billing_panel import BillingPanelState, MemberRow
from daimon.core.channel_budget import ChannelBudgetStatus
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.promo_codes import build_promo_code_terms, hash_promo_code, normalize_promo_code
from daimon.core.promo_credit import ActiveTimedCredit
from daimon.core.stores import promo_codes as promo_store
from daimon.core.stores.domain import ChannelBudgetRow
from daimon.core.stores.platform_names import (
    KnownName,
    get_channel_names,
    get_user_names,
    upsert_channel_names,
    upsert_user_names,
)
from daimon.core.stores.teams_installations import record_teams_installation
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_channel_budget, make_usage_event
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
    assert "Top spenders" in admin and "Top spenders" not in member


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
    assert "Redeemed **$10.00** of credit" in card and "Top spenders" in card


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


def test_the_admin_card_lists_channel_budgets_by_name_and_the_member_card_does_not() -> None:
    budgets = tuple(_budget_status(f"19:c{i}", str(9 - i)) for i in range(7))
    names = {f"19:c{i}": f"Room *{i}*" for i in range(7)} | {"19:c3": ""}
    dump = functools.partial(json.dumps, ensure_ascii=False)
    card = panel_card(_state(channel_budgets=budgets), since=JAN, channel_names=names)
    admin = dump(card.model_dump(by_alias=True))
    assert "Room \\\\*0\\\\*  $9.00 of $10.00 used this month" in admin, "the name, escaped"
    assert "Channel name unavailable  $6.00" in admin, "a channel nobody named reads so"
    assert "19:c" not in admin, "never the raw id"
    assert "Room \\\\*4\\\\*" in admin and "Room \\\\*5\\\\*" not in admin, "five"
    assert "+ 2 more" in admin
    member_state = _state(is_admin=False, channel_budgets=budgets)
    member = dump(
        panel_card(member_state, since=JAN, channel_names=names).model_dump(by_alias=True)
    )
    assert "Channel budgets" not in member and "Room" not in member, "a member sees none"


async def test_channel_labels_list_live_then_stored_then_general() -> None:
    listed: list[str] = []

    async def team_channels(team_id: str) -> dict[str, str]:
        listed.append(team_id)
        if team_id == TEAM_B:
            await asyncio.sleep(10)
        return {TEAM_A: "General", "19:research": "Research"}

    async with asyncio.timeout(2):
        labels, found = await channel_labels(
            team_channels,
            team_ids=[TEAM_A, TEAM_B],
            channel_ids=["19:research", "19:old", TEAM_B, "19:gone"],
            stored={"19:old": "Old Name", "19:research": "Stale"},
            timeout_s=0.1,
        )

    assert labels == {
        "19:research": "Research",
        "19:old": "Old Name",
        TEAM_B: "General",
        "19:gone": "Channel name unavailable",
    }, "live, else stored, else General for a team's own id; never the id"
    assert found == {TEAM_A: "General", "19:research": "Research"}, "what was listed, to remember"
    assert sorted(listed) == sorted([TEAM_A, TEAM_B]), "every installed team, side by side"


# ---------------------------------------------------------------------------
# Top spender names and the credit layout
# ---------------------------------------------------------------------------

JAN = datetime(2026, 1, 1, tzinfo=UTC)
TEAM_A = "19:team-a@thread.tacv2"
TEAM_B = "19:team-b@thread.tacv2"


async def test_the_admin_card_names_top_spenders_and_channels(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    gone, never = str(uuid.UUID(int=7)), str(uuid.UUID(int=8))
    tenant_id = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
    async with db_session_factory.begin() as session:
        await record_teams_installation(
            session, tenant_id=tenant_id, team_id=TEAM_A, group_id="g", name="A"
        )
        tenant = await get_tenant(session, tenant_id)
        assert tenant is not None
        for user, tokens in ((OTHER_AAD_OBJECT_ID, 3000), (gone, 2000), (never, 1000)):
            await make_usage_event(
                session, tenant=tenant, platform_user_id=user, input_tokens=tokens
            )
        await upsert_user_names(
            session, tenant_id=tenant_id, platform="teams", names={gone: KnownName("Priya N.")}
        )
        for channel in (TEAM_A, "19:research@thread.tacv2", "19:old@thread.tacv2"):
            await make_channel_budget(session, tenant=tenant, platform="teams", channel_id=channel)
        await upsert_channel_names(
            session, tenant_id=tenant_id, platform="teams", names={"19:old@thread.tacv2": "Old"}
        )
    teams_api_fake.names[OTHER_AAD_OBJECT_ID] = "Maya *Chen*"
    teams_api_fake.absent.update({gone, never})
    teams_api_fake.channels["19:research@thread.tacv2"] = "Research"

    async with _running(db_session_factory, teams_api_fake) as service:
        admin = await _command(service, teams_api_fake, AAD_OBJECT_ID)
        await platform_names.settle()

    assert "1. Maya \\\\*Chen\\\\*  $" in admin, "a roster name, its markdown escaped"
    assert "2. Priya N.  $" in admin, "someone who left shows the name stored for them"
    assert "3. Name unavailable  $" in admin, "someone never named to us"
    assert "User " not in admin
    assert "<at>" not in admin and '"mention"' not in admin, "a name is never a mention"
    for label in ("General  $", "Research  $", "Old  $"):
        assert label in admin, f"{label!r}: listed, General's filled in, else stored"
    assert "19:" not in admin.split('"Channel budgets"', 1)[1], "never a raw channel id"
    looked_up = sorted(
        r.url.rsplit("/conversations/", 1)[-1]
        for r in teams_api_fake.requests
        if r.url.endswith((OTHER_AAD_OBJECT_ID, gone)) and f"/conversations/{TEAM_A}/" in r.url
    )
    assert looked_up == [f"{TEAM_A}/members/{OTHER_AAD_OBJECT_ID}", f"{TEAM_A}/members/{gone}"], (
        "each shown spender is looked up once, on the installed team's roster"
    )
    async with db_session_factory() as session:
        stored = await get_user_names(
            session, tenant_id=tenant_id, platform="teams", user_ids=[OTHER_AAD_OBJECT_ID]
        )
        channels = await get_channel_names(
            session, tenant_id=tenant_id, platform="teams", channel_ids=["19:research@thread.tacv2"]
        )
    assert stored == {OTHER_AAD_OBJECT_ID: KnownName("Maya *Chen*")}, "a roster name is stored"
    assert channels == {"19:research@thread.tacv2": "Research"}, "a listed channel name is stored"


async def test_a_message_remembers_its_senders_name(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    tenant_id = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
    activity = make_message_activity(text="billing")
    activity["from"] = {**cast(dict[str, Any], activity["from"]), "name": "Maya Chen"}

    async with _running(db_session_factory, teams_api_fake) as service:
        await post_activity(service, activity)
        async with asyncio.timeout(10):
            while not teams_api_fake.activity_requests:
                await asyncio.sleep(0.01)
        await platform_names.settle()

    async with db_session_factory() as session:
        stored = await get_user_names(
            session, tenant_id=tenant_id, platform="teams", user_ids=[AAD_OBJECT_ID]
        )
    assert stored == {AAD_OBJECT_ID: KnownName("Maya Chen")}


async def test_a_channel_message_remembers_the_channels_name(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
    inbound = TeamsInbound(
        kind="channel",
        entra_tenant_id=ENTRA_TENANT_ID,
        user_id=AAD_OBJECT_ID,
        conversation_id="19:research@thread.tacv2;messageid=1",
        channel_id="19:research@thread.tacv2",
        activity_id="1",
        text="hi",
        service_url=None,
        user_name="Maya Chen",
        channel_name="Research",
    )

    remember_inbound(db_session_factory, tenant_id, inbound)
    await platform_names.settle()

    async with db_session_factory() as session:
        channels = await get_channel_names(
            session, tenant_id=tenant_id, platform="teams", channel_ids=[inbound.channel_id]
        )
    assert channels == {inbound.channel_id: "Research"}


async def test_roster_names_tries_each_team_then_the_stored_name() -> None:
    rosters = {TEAM_A: {"u1": "Ann"}, TEAM_B: {"u2": "Bo"}}

    async def roster_name(team_id: str, user_id: str) -> str | None:
        if user_id == "u4":
            raise httpx.HTTPStatusError(
                "gone", request=httpx.Request("GET", "x"), response=httpx.Response(404)
            )
        return rosters[team_id].get(user_id)

    labels, found = await roster_names(
        roster_name,
        team_ids=[TEAM_A, TEAM_B],
        user_ids=["u1", "u2", "u3", "u4"],
        stored={"u1": "Ann (old)", "u4": "Di"},
    )

    assert labels == {"u1": "Ann", "u2": "Bo", "u4": "Di"}, (
        "the first roster with a name wins; a 404 falls back to the stored name"
    )
    assert found == {"u1": KnownName("Ann"), "u2": KnownName("Bo")}, "roster names to remember"


async def test_roster_names_show_the_stored_name_for_a_slow_lookup() -> None:
    async def roster_name(team_id: str, user_id: str) -> str | None:
        if user_id == "slow":
            await asyncio.sleep(10)
        return f"name-{user_id}"

    async with asyncio.timeout(2):
        labels, _ = await roster_names(
            roster_name,
            team_ids=[TEAM_A],
            user_ids=["fast", "slow"],
            stored={"slow": "Sam (stored)"},
            timeout_s=0.1,
        )

    assert labels == {"fast": "name-fast", "slow": "Sam (stored)"}


async def test_roster_names_without_an_installed_team_uses_the_stored_names() -> None:
    async def roster_name(team_id: str, user_id: str) -> str | None:
        raise AssertionError("no lookup without a team")

    labels, found = await roster_names(
        roster_name, team_ids=[], user_ids=["u1", "u2"], stored={"u1": "Ann"}
    )
    assert labels == {"u1": "Ann"} and found == {}


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
        member_rows=(MemberRow("u", "Ann Lee", 1.0, 1, True),),
        over_cap_count=0,
    )
    return dataclasses.replace(base, **overrides)  # pyright: ignore[reportArgumentType]


def _body(card: Any) -> list[dict[str, Any]]:
    return card.model_dump(by_alias=True, exclude_none=True)["body"]


def _texts(element: dict[str, Any]) -> list[str]:
    if element["type"] == "TextBlock":
        return [element["text"]]
    if element["type"] == "Container":
        return [text for item in element["items"] for text in _texts(item)]
    return []


def _timed(*days: int) -> tuple[ActiveTimedCredit, ...]:
    return tuple(
        ActiveTimedCredit(Decimal(day), datetime(2026, 1, day, tzinfo=UTC)) for day in days
    )


def test_the_admin_card_is_sections_set_apart_with_the_total_biggest() -> None:
    state = _state(guild_spend=48.17, guild_distinct_members=9, timed_credit=_timed(20, 5))
    body = _body(panel_card(state, since=JAN))
    header, credit, spenders, expiry, actions = body
    assert _texts(spenders) == ["Top spenders", "1. Ann Lee (you)  $1.00"]
    assert _texts(header) == ["Billing", "January 2026", "$48.17 spent by 9 people"]
    assert credit["separator"] is True and credit["spacing"] == "Large"
    total = credit["items"][0]
    assert (
        total["text"] == "$62.40" and total["size"] == "ExtraLarge" and total["weight"] == "Bolder"
    )
    assert _texts(credit)[1:] == [
        "total credit left",
        "Includes $25.00 that expires. It's used first.",
    ]
    assert credit["items"][2]["isSubtle"] is True, "the timed credit line is grey detail"
    assert expiry["id"] == "billing-expiry" and expiry["isVisible"] is False
    assert _texts(expiry) == [
        "Unused credit expires:",
        "$5.00 on {{DATE(2026-01-05T00:00:00Z, SHORT)}}",
        "$20.00 on {{DATE(2026-01-20T00:00:00Z, SHORT)}}",
    ], "soonest first, hidden until Expiry dates is pressed"
    titles = [action["title"] for action in actions["actions"]]
    assert titles == ["Add credit", "Expiry dates"]
    add, toggle = actions["actions"]
    assert add["type"] == "Action.ShowCard"
    amounts = add["card"]["body"][0]
    assert amounts["type"] == "ColumnSet" and len(amounts["columns"]) == 4
    first = amounts["columns"][0]["items"]
    assert first[0]["actions"][0]["title"] == "$10", "the button is the amount alone"
    assert first[1]["text"] == "about 100 turns" and first[1]["isSubtle"] is True, (
        "what it buys sits under it in grey"
    )
    assert toggle["type"] == "Action.ToggleVisibility"
    assert toggle["targetElements"] == ["billing-expiry"]


def test_redeem_code_is_offered_only_while_a_code_is_redeemable() -> None:
    since = JAN
    titles = [
        a["title"]
        for a in _body(panel_card(_state(has_redeemable_promo_code=True), since=since))[-1][
            "actions"
        ]
    ]
    assert titles == ["Add credit", "Redeem code"]


def test_the_member_card_shows_own_use_and_asks_an_admin_for_credit() -> None:
    state = _state(is_admin=False, caller_spend=11.5, caller_cap=Decimal("25"))
    body = _body(panel_card(state, since=JAN))
    assert [_texts(element) for element in body] == [
        ["Billing", "January 2026"],
        ["You", "$11.50 of your $25.00 this month"],
        ["$62.40", "total credit left", "Ask an admin to add credit."],
    ], "no actions without timed credit"
    with_timed = _body(panel_card(_state(is_admin=False, timed_credit=_timed(20)), since=JAN))
    assert [a["title"] for a in with_timed[-1]["actions"]] == ["Expiry dates"]


def test_a_negative_balance_says_no_credit_left_and_still_shows_timed_credit() -> None:
    state = _state(is_admin=False, guild_balance_usd=Decimal("-3"), timed_credit=_timed(20))
    credit = _body(panel_card(state, since=JAN))[2]
    assert _texts(credit) == [
        "No credit left",
        "$3.00 spent beyond it",
        "Includes $20.00 that expires. It's used first.",
        "Ask an admin to add credit.",
    ]


def test_this_channels_budget_has_its_own_section() -> None:
    status = _budget_status("19:c", "1.2")
    body = _body(panel_card(_state(channel_budget=status), since=JAN))
    assert _texts(body[2]) == ["This channel", "$1.20 of $10.00 used this month"]
    assert body[2]["separator"] is True


def test_top_spenders_names_five_and_counts_the_rest() -> None:
    rows = tuple(MemberRow(f"u{i}", f"P{i}", float(9 - i), 1, i == 1) for i in range(7))
    body = _body(panel_card(_state(member_rows=rows, over_cap_count=1), since=JAN))
    assert _texts(body[2]) == [
        "Top spenders",
        "1. P0  $9.00",
        "2. P1 (you)  $8.00",
        "3. P2  $7.00",
        "4. P3  $6.00",
        "5. P4  $5.00",
        "+ 3 more",
    ]


def test_no_rendered_card_has_a_dot_separator_or_a_user_label() -> None:
    rows = tuple(
        MemberRow(f"u{i}", None if i % 2 else f"P{i}", float(9 - i), 1, False) for i in range(7)
    )
    for is_admin in (True, False):
        state = _state(
            is_admin=is_admin,
            member_rows=rows,
            guild_spend=48.17,
            guild_turns=300,
            guild_distinct_members=9,
            timed_credit=_timed(5, 20),
            has_redeemable_promo_code=True,
            channel_budget=_budget_status("19:c", "1.2"),
            channel_budgets=(_budget_status("19:c", "1.2"),),
        )
        text = json.dumps(_body(panel_card(state, since=JAN)), ensure_ascii=False)
        assert "·" not in text and "≈" not in text, text
        assert "User " not in text and "19:c" not in text, text
