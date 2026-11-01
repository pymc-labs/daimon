"""`/api/credits/*` as the web app calls it: shapes, rights, allowances and top-ups."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from daimon.core.billing import BillingConfig
from daimon.core.config import Settings
from daimon.core.stores import accounts as accounts_store
from daimon.core.stores import spend_allowances, tenant_ledger, usage_events, web_audit
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_roles as roles_store
from daimon.core.stores.domain import WebPersonRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.turn.outcomes import drain_outcomes
from daimon.testing.factories import make_platform_principal
from daimon.testing.turn_router import AGENT_ID, build_turn_router
from mux.testing.drivers.anthropic.ma_models import DEFAULT_AGENT_NAME, ma_model_usage
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import (
    Workspace,
    build_turns,
    funded_workspace,
    one_session,
    shown_trail,
    signed_in,
    sse_frames,
)
from .conftest import PUBLIC_URL, WebClientFactory, web_settings

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
_ADMIN_ROUTES = [
    ("GET", "/api/credits/overview", None),
    ("GET", "/api/credits/spend?by=person", None),
    ("GET", "/api/credits/allowances", None),
    (
        "POST",
        "/api/credits/allowances",
        {
            "subject": {"kind": "group", "id": "everyone", "name": ""},
            "amount_usd": 5,
            "window": "month",
        },
    ),
    ("DELETE", f"/api/credits/allowances/{uuid.uuid4()}", None),
    ("POST", "/api/credits/topup", {"amount_usd": 10}),
]


def _settings(markup: str = "1.0") -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        billing={"markup": markup},  # pyright: ignore[reportArgumentType]
    )


def _stripe(created: list[dict[str, Any]]) -> Any:
    async def create_async(params: dict[str, Any]) -> SimpleNamespace:
        created.append(params)
        return SimpleNamespace(id="cs_1", url="https://checkout.example.test/cs_1")

    sessions = SimpleNamespace(create_async=create_async)
    return SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(sessions=sessions)))


_BILLING = BillingConfig(
    secret_key=SecretStr("sk_test"),
    webhook_secret=SecretStr("whsec_test"),
    prices={10: "price_10", 25: "price_25", 50: "price_50", 100: "price_100"},
    success_url="https://mcp.example.test/billing/success",
    cancel_url="https://mcp.example.test/billing/cancel",
)


async def _add_member(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    name: str,
    *,
    discord_id: str | None = None,
) -> WebPersonRow:
    """A web member; with `discord_id`, one who linked that Discord account."""
    person_id = uuid.uuid4()
    async with factory.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform="web", external_id=str(person_id)
        )
        if discord_id is not None:
            tenant = await tenants_store.get_tenant(session, tenant_id)
            account = await accounts_store.get_account(session, principal.account_id)
            await make_platform_principal(
                session, platform="discord", external_id=discord_id, tenant=tenant, account=account
            )
        return await people_store.upsert_member(
            session,
            tenant_id=tenant_id,
            account_id=principal.account_id,
            display_name=name,
            workspace_name="Acme",
            email=f"{name.lower()}@example.com",
            new_person_id=person_id,
        )


async def _spend(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    user: str,
    agent: str | None = AGENT_ID,
    n: int,
) -> None:
    """One $5 Opus call (a million input tokens) for `user` on `agent`."""
    async with factory.begin() as session:
        await usage_events.record(
            session,
            tenant_id=tenant_id,
            platform_user_id=user,
            managed_session_id=f"s-{tenant_id}-{n}",
            model="claude-opus-4-7",
            model_usage=ma_model_usage(input_tokens=1_000_000, output_tokens=0),
            event_id=f"evt-{n}",
            agent_id=agent,
        )


class Rig:
    """An owner and a member in one funded workspace, talking to the app."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        web_client: WebClientFactory,
    ) -> None:
        self.factory, self.tmp_path, self.web_client = factory, tmp_path, web_client

    @asynccontextmanager
    async def open(
        self,
        *,
        as_owner: bool = True,
        discord_id: str | None = None,
        web: dict[str, Any] | None = None,
        **options: Any,
    ) -> AsyncIterator[tuple[httpx.AsyncClient, Workspace, WebPersonRow]]:
        ws = await funded_workspace(self.factory)
        member = await _add_member(self.factory, ws.tenant_id, "Mia", discord_id=discord_id)
        async with self.factory.begin() as db:
            await roles_store.set_workspace_role(
                db,
                tenant_id=ws.tenant_id,
                account_id=ws.person.account_id,
                role="owner",
                granted_by_account_id=None,
            )
        turns = build_turns(self.factory, build_turn_router(str(ws.tenant_id)), self.tmp_path)
        token = await signed_in(self.factory, ws.person if as_owner else member)
        async with self.web_client(
            web_settings(ws.tenant_id, **(web or {})),
            sessionmaker=self.factory,
            turns=turns,
            **options,
        ) as client:
            client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
            client.headers.update(_OWN)
            yield client, ws, member


async def test_members_are_refused_every_admin_route_but_read_their_own(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    rig = Rig(db_session_factory, tmp_path, web_client)
    async with rig.open(as_owner=False) as (client, _, _):
        for method, path, body in _ADMIN_ROUTES:
            res = await client.request(method, path, json=body)
            assert res.status_code == 403, f"{method} {path} must be admin only: {res.text}"
        mine = await client.get("/api/credits/mine")
    assert mine.status_code == 200, "a member reads their own meter"


async def test_signed_out_callers_are_refused(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    rig = Rig(db_session_factory, tmp_path, web_client)
    async with rig.open() as (client, _, _):
        client.cookies.clear()
        res = await client.get("/api/credits/mine")
    assert res.status_code == 401, "no session, no credits"


async def test_mine_is_the_callers_billed_spend_by_agent(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    rig = Rig(db_session_factory, tmp_path, web_client)
    async with rig.open(settings=_settings("2")) as (client, ws, member):
        await _spend(db_session_factory, ws.tenant_id, user=str(ws.person.id), n=1)
        await _spend(db_session_factory, ws.tenant_id, user=str(member.id), n=2)
        mine = (await client.get("/api/credits/mine")).json()
    assert mine == {
        "by_agent": [
            {"id": AGENT_ID, "name": DEFAULT_AGENT_NAME, "avatar": "", "turns": 1, "usd": 10.0}
        ],
        "turns": [],
        "spent_usd": 10.0,
        "turn_count": 1,
    }, "$5 of model cost at a 2x markup is $10 billed, and another person's spend is left out"


async def test_overview_has_the_shape_the_app_reads(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    rig = Rig(db_session_factory, tmp_path, web_client)
    async with rig.open(settings=_settings("2")) as (client, ws, _):
        await _spend(db_session_factory, ws.tenant_id, user=str(ws.person.id), n=1)
        async with db_session_factory.begin() as db:
            await tenant_ledger.insert_entry(
                db,
                tenant_id=ws.tenant_id,
                delta_usd=Decimal("25"),
                reason="manual_credit",
                idempotency_key="k1",
                actor_account_id=ws.person.account_id,
            )
        body = (await client.get("/api/credits/overview")).json()
    assert set(body) == {
        "balance_usd", "spent_month_usd", "projected_month_usd", "turns_month",
        "daily", "ledger", "topup",
    }  # fmt: skip
    assert body["balance_usd"] == 125.0, "the $100 welcome credit plus the $25 grant"
    assert (body["spent_month_usd"], body["turns_month"]) == (10.0, 1), "billed, at the markup"
    assert {"day", "usd"} == set(body["daily"][0]) and len(body["daily"]) >= 28
    kinds = {(e["kind"], e["by"], e["note"], e["amount_usd"]) for e in body["ledger"]}
    assert kinds == {
        ("promo", "Daimon", "Welcome credit", 100.0),
        ("topup", "Ada", "Manual grant", 25.0),
    }, "a grant names who made it"
    assert set(body["ledger"][0]) == {"id", "at", "kind", "amount_usd", "by", "note"}
    assert body["topup"] is None, "no card checkout and no manual grants here"


async def test_overview_names_how_credit_can_be_added(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    rig = Rig(db_session_factory, tmp_path, web_client)
    async with rig.open(web={"manual_topup": True}) as (client, _, _):
        manual = (await client.get("/api/credits/overview")).json()["topup"]
    async with rig.open(
        web={"manual_topup": True}, billing_config=_BILLING, stripe=_stripe([])
    ) as (client, _, _):
        checkout = (await client.get("/api/credits/overview")).json()["topup"]
    assert (manual, checkout) == ("manual", "checkout"), "a card checkout wins over a grant"


async def test_spend_lists_billed_rows_by_person_agent_and_channel(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    rig = Rig(db_session_factory, tmp_path, web_client)
    async with rig.open(settings=_settings("2")) as (client, ws, member):
        await _spend(db_session_factory, ws.tenant_id, user=str(ws.person.id), n=1)
        await _spend(db_session_factory, ws.tenant_id, user=str(ws.person.id), n=2)
        await _spend(db_session_factory, ws.tenant_id, user=str(member.id), agent=None, n=3)
        people = (await client.get("/api/credits/spend?by=person")).json()
        agents = (await client.get("/api/credits/spend?by=agent&period=this")).json()
        channels = (await client.get("/api/credits/spend?by=channel")).json()
        bad = await client.get("/api/credits/spend?by=colour")
    assert people == [
        {"id": str(ws.person.id), "name": "Ada", "turns": 2, "usd": 20.0},
        {"id": str(member.id), "name": "Mia", "turns": 1, "usd": 10.0},
    ], "people are named from their web profile, biggest spender first"
    assert agents == [
        {"id": AGENT_ID, "name": DEFAULT_AGENT_NAME, "avatar": "", "turns": 2, "usd": 20.0},
        {"id": "other", "name": "Other", "avatar": "", "turns": 1, "usd": 10.0},
    ], "spend with no agent recorded stays visible"
    assert channels == [], "no turn has finished, so no channel has spend"
    assert bad.status_code == 422, "an unknown grouping is refused"


async def test_spend_last_month_leaves_out_this_months_spend(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    rig = Rig(db_session_factory, tmp_path, web_client)
    async with rig.open() as (client, ws, _):
        await _spend(db_session_factory, ws.tenant_id, user=str(ws.person.id), n=1)
        last = await client.get("/api/credits/spend?by=person&period=last")
        this = (await client.get("/api/credits/spend?by=person&period=this")).json()
    assert last.json() == [], "what is spent now is not last month's"
    assert [(r["name"], r["usd"]) for r in this] == [("Ada", 5.0)], "it is this month's"


async def test_allowances_are_added_listed_and_removed(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    rig = Rig(db_session_factory, tmp_path, web_client)
    async with rig.open() as (client, ws, member):
        person = await client.post(
            "/api/credits/allowances",
            json={
                "subject": {"kind": "person", "id": str(member.id), "name": "Mia"},
                "agent": {"id": AGENT_ID},
                "amount_usd": 20,
                "window": "month",
                "expires_at": None,
            },
        )
        group = await client.post(
            "/api/credits/allowances",
            json={
                "subject": {"kind": "group", "id": "everyone", "name": "Everyone"},
                "agent": None,
                "amount_usd": 40.5,
                "window": "total",
                "expires_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
            },
        )
        # An allowance counts what is spent once it exists.
        await _spend(db_session_factory, ws.tenant_id, user=str(member.id), n=1)
        listed = (await client.get("/api/credits/allowances")).json()
        removed = await client.delete(f"/api/credits/allowances/{person.json()['id']}")
        again = await client.delete(f"/api/credits/allowances/{person.json()['id']}")
        after = (await client.get("/api/credits/allowances")).json()
    assert person.status_code == 200 and group.status_code == 200, (person.text, group.text)
    mia = listed[1]
    assert mia["subject"] == {"kind": "person", "id": str(member.id), "name": "Mia"}
    assert mia["agent"]["id"] == AGENT_ID and mia["agent"]["name"] == DEFAULT_AGENT_NAME
    assert (mia["amount_usd"], mia["used_usd"], mia["window"]) == (20.0, 5.0, "month")
    assert mia["expires_at"] is None and "members" not in mia, "members is for groups only"
    everyone = listed[0]
    assert everyone["subject"] == {"kind": "group", "id": "everyone", "name": "Everyone"}
    assert (everyone["agent"], everyone["amount_usd"], everyone["members"]) == (None, 40.5, 2)
    assert everyone["expires_at"] is not None, "the newest allowance comes first"
    assert removed.json() == {"ok": True} and again.status_code == 404
    assert [a["subject"]["kind"] for a in after] == ["group"], "the removed one is gone"
    async with db_session_factory() as db:
        [row] = await spend_allowances.list_for_tenant(db, tenant_id=ws.tenant_id)
    assert row.created_by_account_id == ws.person.account_id, "who set it is recorded"
    trail = await shown_trail(db_session_factory, ws.tenant_id)
    assert trail == [
        ("Added allowance", "Mia", f"$20.00 a month, {DEFAULT_AGENT_NAME}"),
        ("Added allowance", "Everyone", "$40.50 in total"),
        ("Removed allowance", "Mia", f"$20.00 a month, {DEFAULT_AGENT_NAME}"),
    ], "every allowance change is in the audit trail"


async def test_a_person_allowance_is_kept_under_the_id_their_turns_run_as(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    rig = Rig(db_session_factory, tmp_path, web_client)
    async with rig.open(discord_id="d-77") as (client, ws, member):
        made = await client.post(
            "/api/credits/allowances",
            json={
                "subject": {"kind": "person", "id": str(member.id), "name": "Mia"},
                "amount_usd": 20,
                "window": "month",
            },
        )
        assert made.is_success, "the allowance is created"
        await _spend(db_session_factory, ws.tenant_id, user="d-77", n=1)
        await _spend(db_session_factory, ws.tenant_id, user=str(member.id), n=2)
        row = (await client.get("/api/credits/allowances")).json()[0]
    async with db_session_factory() as db:
        [stored] = await spend_allowances.list_for_tenant(db, tenant_id=ws.tenant_id)
    assert stored.subject_id == "d-77", "the gate matches on the linked chat account's id"
    assert row["subject"] == {"kind": "person", "id": str(member.id), "name": "Mia"}, (
        "the app still sees the web person"
    )
    assert row["used_usd"] == 10.0, (
        "use counts under every id the person spends as, the web one too"
    )


async def test_allowance_input_is_checked(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    rig = Rig(db_session_factory, tmp_path, web_client)
    group = {"kind": "group", "id": "everyone", "name": ""}
    async with rig.open() as (client, _, _):

        async def post(**fields: Any) -> int:
            body = {"subject": group, "amount_usd": 5, "window": "month", **fields}
            return (await client.post("/api/credits/allowances", json=body)).status_code

        assert await post(amount_usd=0) == 422, "an allowance must be more than nothing"
        assert await post(window="week") == 422, "only month or total"
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        assert await post(expires_at=past) == 422, "an allowance can't already be over"
        stranger = {"kind": "person", "id": str(uuid.uuid4()), "name": "?"}
        assert await post(subject=stranger) == 404, "a person outside the workspace is unknown"


async def test_manual_topup_writes_a_ledger_row_naming_the_actor(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    rig = Rig(db_session_factory, tmp_path, web_client)
    async with rig.open(web={"manual_topup": True}) as (client, ws, _):
        body = {"amount_usd": 12.5, "request_id": "r1"}
        first = await client.post("/api/credits/topup", json=body)
        replay = await client.post("/api/credits/topup", json=body)
    assert first.json() == {"balance_usd": 112.5} and replay.json() == first.json(), (
        "the same request id credits once"
    )
    async with db_session_factory() as db:
        rows = [
            r
            for r in await tenant_ledger.list_for_tenant(db, tenant_id=ws.tenant_id)
            if r.reason == "manual_credit"
        ]
    assert [(r.delta_usd, r.actor_account_id) for r in rows] == [
        (Decimal("12.50"), ws.person.account_id)
    ], "one manual credit, made by the signed-in admin"
    async with db_session_factory() as db:
        trail = await web_audit.list_recent(db, tenant_id=ws.tenant_id)
    assert [(a.action, a.detail, a.actor_account_id) for a in trail] == [
        ("Topped up", "$12.50", ws.person.account_id)
    ], "a top-up is audited once, however often it is replayed"


async def test_manual_topup_needs_a_request_id(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    rig = Rig(db_session_factory, tmp_path, web_client)
    async with rig.open(web={"manual_topup": True}) as (client, ws, _):
        missing = await client.post("/api/credits/topup", json={"amount_usd": 12.5})
        blank = await client.post("/api/credits/topup", json={"amount_usd": 12.5, "request_id": ""})
    assert (missing.status_code, blank.status_code) == (422, 422), (
        "a manual credit with no request id could be replayed into a double credit"
    )
    async with db_session_factory() as db:
        credits = [
            r
            for r in await tenant_ledger.list_for_tenant(db, tenant_id=ws.tenant_id)
            if r.reason == "manual_credit"
        ]
    assert credits == [], "nothing was credited"


async def test_checkout_amounts_are_limited_and_return_the_checkout_url(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    rig = Rig(db_session_factory, tmp_path, web_client)
    created: list[dict[str, Any]] = []
    async with rig.open(billing_config=_BILLING, stripe=cast(Any, _stripe(created))) as (
        client,
        ws,
        _,
    ):
        refused = [
            (await client.post("/api/credits/topup", json={"amount_usd": amount})).status_code
            for amount in (7, 10.5, 200)
        ]
        ok = await client.post("/api/credits/topup", json={"amount_usd": 25})
    assert refused == [422, 422, 422], "only $10, $25, $50 and $100 may be bought"
    assert ok.json() == {"checkout_url": "https://checkout.example.test/cs_1"}
    assert len(created) == 1, "refused amounts never reach Stripe"
    assert created[0]["line_items"] == [{"price": "price_25", "quantity": 1}]
    assert created[0]["metadata"] == {"tenant_id": str(ws.tenant_id)}
    assert created[0]["success_url"] == f"{PUBLIC_URL}/credits?topup=ok", "returns to the web app"


async def test_topup_is_not_found_when_nothing_can_add_credit(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    rig = Rig(db_session_factory, tmp_path, web_client)
    async with rig.open() as (client, _, _):
        res = await client.post("/api/credits/topup", json={"amount_usd": 10})
    assert res.status_code == 404, "no checkout and no manual grants: nothing to add credit with"


async def test_me_credit_is_the_meter_of_the_allowance_that_governs_the_caller(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    rig = Rig(db_session_factory, tmp_path, web_client)
    async with rig.open() as (client, ws, _):
        before = (await client.get("/api/me")).json()["credit"]
        async with db_session_factory.begin() as db:
            await spend_allowances.create_allowance(
                db,
                tenant_id=ws.tenant_id,
                subject_kind="group",
                subject_id="everyone",
                agent_id=None,
                amount_usd=Decimal("20"),
                window="month",
                starts_at=datetime.now(UTC) - timedelta(days=1),
            )
        await _spend(db_session_factory, ws.tenant_id, user=str(ws.person.id), n=1)
        after = (await client.get("/api/me")).json()["credit"]
    assert before is None, "nothing limits a member with no allowance"
    assert set(after) == {"left_usd", "total_usd", "window", "resets_at"}
    assert (after["left_usd"], after["total_usd"], after["window"]) == (15.0, 20.0, "month")
    assert after["resets_at"] is not None, "a monthly allowance says when it renews"


async def test_one_turn_is_the_same_charge_in_the_reply_the_meter_and_every_total(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Sources: the reply reads the turn's own usage samples; the meter and the month total
    read the usage events; the chart reads the ledger debits; recent turns and the agent
    split read the turn outcome."""
    ws = await funded_workspace(db_session_factory)
    async with db_session_factory.begin() as db:
        await roles_store.set_workspace_role(
            db,
            tenant_id=ws.tenant_id,
            account_id=ws.person.account_id,
            role="owner",
            granted_by_account_id=None,
        )
        await spend_allowances.create_allowance(
            db,
            tenant_id=ws.tenant_id,
            subject_kind="person",
            subject_id=str(ws.person.id),
            agent_id=None,
            amount_usd=Decimal("20"),
            window="total",
            starts_at=datetime.now(UTC) - timedelta(days=1),
        )
    router = build_turn_router(str(ws.tenant_id), input_tokens=1_234_567, output_tokens=0)
    turns = build_turns(db_session_factory, router, tmp_path)
    token = await signed_in(db_session_factory, ws.person)
    with one_session():
        async with web_client(
            web_settings(ws.tenant_id), sessionmaker=db_session_factory, turns=turns
        ) as client:
            client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
            client.headers.update(_OWN)
            sid = (await client.post("/api/chat/sessions", json={"agent_id": AGENT_ID})).json()[
                "id"
            ]
            sent = await client.post(
                f"/api/chat/sessions/{sid}/messages",
                json={"id": str(uuid.uuid4()), "content": "hi"},
            )
            await turns.drain(10)
            await drain_outcomes()
            streamed = sse_frames(sent.text)[-1][1]["message"]
            saved = [
                m
                for m in (await client.get(f"/api/chat/sessions/{sid}")).json()["messages"]
                if m["role"] == "assistant"
            ]
            mine = (await client.get("/api/credits/mine")).json()
            overview = (await client.get("/api/credits/overview")).json()
            meter = (await client.get("/api/me")).json()["credit"]

    assert streamed["cost_usd"] == pytest.approx(3.703701), "the reply carries the turn's cost"
    assert [m["cost_usd"] for m in saved] == [streamed["cost_usd"]], "and keeps it once saved"
    assert mine["turns"][0]["usd"] == 3.7, "recent turns show it to the cent"
    assert (mine["spent_usd"], mine["by_agent"][0]["usd"]) == (3.7, 3.7)
    assert overview["spent_month_usd"] == 3.7, "the month total"
    assert sum(d["usd"] for d in overview["daily"]) == 3.7, "the chart adds up to the total"
    assert meter["left_usd"] == pytest.approx(20 - 3.703701), "the meter counts the same turn"
