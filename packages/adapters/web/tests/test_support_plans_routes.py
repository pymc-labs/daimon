"""`/api/support-plans`: admins change plans, menus and caps; stale values, refusals, audit."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from daimon.core.platform_names import ChannelSighting
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores.platform_names import upsert_channel_names
from daimon.core.support_plans import RequestChoice, SupportPlace
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import shown_trail
from .conftest import WebClientFactory
from .test_admin_routes import Admin, signed_in_admin

type Factory = async_sessionmaker[AsyncSession]

_P1, _P2 = "100000000000000001", "100000000000000002"
_OPTION: dict[str, object] = {
    "key": "review",
    "label": "Review",
    "price": {"kind": "range", "min": "1", "max": "3"},
}
_MENU: dict[str, object] = {"name": "Support", "options": [_OPTION]}
_PLAN: dict[str, object] = {"catalog": "support", "pool": "5", "per_person": "2"}
_ROUTES: list[tuple[str, str, dict[str, object] | None]] = [
    ("GET", "/api/support-plans", None),
    ("PUT", "/api/support-plans/plan", {"scope": "workspace", "plan": _PLAN, "expected": None}),
    ("PUT", "/api/support-plans/catalogs/support", {"catalog": _MENU, "expected_revision": None}),
    (
        "PUT",
        "/api/support-plans/allowances",
        {"platform_user_id": _P1, "credits": "4", "expected": None},
    ),
]


@asynccontextmanager
async def _support(
    web_client: WebClientFactory, factory: Factory, tmp_path: Path
) -> AsyncIterator[Admin]:
    """The admin fixture with channel `c1` named general and thread `t9` under it."""
    async with signed_in_admin(web_client, factory, tmp_path) as admin:
        async with factory.begin() as db:
            await upsert_channel_names(
                db,
                tenant_id=admin.ws.tenant_id,
                platform="discord",
                names={
                    "c1": ChannelSighting("general"),
                    "t9": ChannelSighting("Launch", kind="thread", parent_id="c1"),
                },
            )
        yield admin


async def test_admin_sets_a_menu_a_thread_plan_and_a_cap_then_reads_them_back(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        put = admin.client.put
        menu = await put(
            "/api/support-plans/catalogs/support",
            json={"catalog": _MENU, "expected_revision": None},
        )
        plan = await put(
            "/api/support-plans/plan",
            json={"scope": "channel", "scope_id": "t9", "plan": _PLAN, "expected": None},
        )
        cap = await put(
            "/api/support-plans/allowances",
            json={
                "platform_user_id": _P1,
                "credits": "4",
                "scope": "channel",
                "scope_id": "c1",
                "expected": None,
            },
        )
        default_cap = await put(
            "/api/support-plans/allowances",
            json={"platform_user_id": f"<@{_P2}>", "credits": "0.5", "expected": None},
        )
        trail = await shown_trail(db_session_factory, admin.ws.tenant_id)

    assert menu.status_code == 200, menu.text
    assert menu.json()["catalogs"] == [
        {
            "key": "support",
            "name": "Support",
            "revision": 1,
            "summary": "Review (1-3 credits)",
            "catalog": {**_MENU, "options": [{**_OPTION, "fields": []}]},
        }
    ], "the menu in one line and as stored"
    assert plan.status_code == 200, plan.text
    assert cap.status_code == 200, cap.text
    page = default_cap.json()
    (shown,) = page["plans"]
    assert (shown["scope_id"], shown["name"], shown["summary"]) == (
        "c1",
        "general",
        "0 of 5 credits used, 2 per person, all time",
    ), "the thread's parent holds the plan, by name"
    assert shown["allowances"] == [{"platform_user_id": _P1, "name": None, "credits": "4"}], (
        "the person's own cap under the plan"
    )
    (own,) = page["default_allowances"]
    assert (own["platform_user_id"], own["credits"]) == (_P2, "0.5"), (
        "a cap where no plan applies, the mention unwrapped"
    )
    assert page["default_credits"] == "20", "the deployment's default count"
    assert trail[-4:] == [
        ("Changed support menu", "support", "Menu"),
        ("Changed support plan", "#general", "Plan"),
        ("Changed support plan", "#general", "Own cap"),
        ("Changed support plan", "Workspace", "Own cap"),
    ], "each write audited, naming no person"


async def test_page_when_a_request_reached_no_channel_then_lists_it_as_undelivered(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        ids: list[uuid.UUID] = []
        for message in ("m1", "m2"):
            async with db_session_factory.begin() as db:
                outcome = await ledger.submit_request(
                    db,
                    tenant_id=admin.ws.tenant_id,
                    account_id=None,
                    platform="discord",
                    platform_user_id="1",
                    channel_id="c1",
                    message_id=message,
                    ma_session_id=None,
                    place=SupportPlace(channel_id="c1", agent_id=None),
                    choice=RequestChoice.default("help"),
                    default_allowance=20,
                )
            assert outcome.row is not None, "recorded"
            ids.append(outcome.row.id)
        async with db_session_factory.begin() as db:
            await ledger.mark_delivered(db, escalation_id=ids[1])
        page = await admin.client.get("/api/support-plans")

    assert page.status_code == 200, page.text
    assert [(r["id"], r["counted"]) for r in page.json()["undelivered"]] == [(str(ids[0]), "1")], (
        "the request nobody received, with what it holds"
    )


async def test_a_stale_value_is_a_409_and_the_value_last_read_goes_through(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        put = admin.client.put
        body: dict[str, object] = {"scope": "workspace", "plan": _PLAN, "expected": None}
        await put(
            "/api/support-plans/catalogs/support",
            json={"catalog": _MENU, "expected_revision": None},
        )
        first = (await put("/api/support-plans/plan", json=body)).json()["plans"][0]
        again = await put("/api/support-plans/plan", json=body)
        changed = await put(
            "/api/support-plans/plan", json={**body, "plan": {"pool": "9"}, "expected": first}
        )
        stale_menu = await put(
            "/api/support-plans/catalogs/support",
            json={"catalog": _MENU, "expected_revision": None},
        )
        cleared = await put(
            "/api/support-plans/plan",
            json={**body, "plan": None, "expected": changed.json()["plans"][0]},
        )
        removed = await put(
            "/api/support-plans/catalogs/support", json={"catalog": None, "expected_revision": 1}
        )

    assert again.status_code == 409, "a plan set since it was read"
    assert changed.status_code == 200, changed.text
    assert stale_menu.status_code == 409, "a menu set since it was read"
    assert cleared.status_code == 200 and cleared.json()["plans"] == [], cleared.text
    assert removed.status_code == 200 and removed.json()["catalogs"] == [], removed.text


async def test_values_that_cannot_be_used_are_refused_and_nothing_is_saved(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        put = admin.client.put
        refused = [
            await put(
                "/api/support-plans/plan",
                json={"scope": "workspace", "plan": {"pool": "-1"}, "expected": None},
            ),
            await put(
                "/api/support-plans/plan",
                json={"scope": "channel", "scope_id": "web", "plan": {}, "expected": None},
            ),
            await put(
                "/api/support-plans/plan",
                json={"scope": "agent", "scope_id": "agent_x", "plan": {}, "expected": None},
            ),
            await put(
                "/api/support-plans/catalogs/support",
                json={"catalog": {"name": "x"}, "expected_revision": None},
            ),
            await put(
                "/api/support-plans/catalogs/Bad Key",
                json={"catalog": _MENU, "expected_revision": None},
            ),
            await put(
                "/api/support-plans/allowances",
                json={"platform_user_id": "u1", "credits": "1", "expected": None},
            ),
        ]
        no_plan = await put(
            "/api/support-plans/allowances",
            json={
                "platform_user_id": _P1,
                "credits": "1",
                "scope": "channel",
                "scope_id": "c1",
                "expected": None,
            },
        )
        page = (await admin.client.get("/api/support-plans")).json()

    assert [r.status_code for r in refused] == [422] * 6, [r.text for r in refused]
    assert "u1 is not a Discord user id" in refused[-1].text, "a cap names the bad id"
    assert no_plan.status_code == 404, "a cap under a plan that does not exist"
    assert (page["plans"], page["catalogs"], page["default_allowances"]) == ([], [], []), (
        "nothing saved"
    )


async def test_members_can_neither_read_nor_change_support_plans(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        admin.as_member()
        refused = {
            path: (await admin.client.request(method, path, json=body)).status_code
            for method, path, body in _ROUTES
        }

    assert set(refused.values()) == {403}, refused


async def test_cap_when_set_from_a_thread_or_for_an_unknown_agent_then_parent_or_refused(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        put = admin.client.put
        await put(
            "/api/support-plans/catalogs/support",
            json={"catalog": _MENU, "expected_revision": None},
        )
        await put(
            "/api/support-plans/plan",
            json={"scope": "channel", "scope_id": "c1", "plan": _PLAN, "expected": None},
        )
        from_thread = await put(
            "/api/support-plans/allowances",
            json={
                "platform_user_id": _P1,
                "credits": "4",
                "scope": "channel",
                "scope_id": "t9",
                "expected": None,
            },
        )
        bad_agent = await put(
            "/api/support-plans/allowances",
            json={
                "platform_user_id": _P1,
                "credits": "4",
                "scope": "agent",
                "scope_id": "agent_x",
                "expected": None,
            },
        )

    assert from_thread.status_code == 200, from_thread.text
    (shown,) = from_thread.json()["plans"]
    assert (shown["scope_id"], shown["allowances"]) == (
        "c1",
        [{"platform_user_id": _P1, "name": None, "credits": "4"}],
    ), "the cap sits on the thread's parent channel, where the plan is"
    assert bad_agent.status_code == 422, "an agent that is not in the workspace"
