"""`/api/admin/dm*`, `/api/admin/platform-groups` and agent access's DM part: shapes,
refusals and an audit row per change that names no person."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
from daimon.core._models import Tenant
from daimon.core.config import DiscordSettings, Settings, TurnSettings
from daimon.core.stores import dm_access as dm_store
from daimon.core.stores import platform_names
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.platform_names import KnownName
from daimon.testing.turn_router import AGENT_ID
from mux.testing.drivers.anthropic.ma_models import DEFAULT_AGENT_NAME
from pydantic import SecretStr
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import FakeUpstream, WebClientFactory
from .test_admin_routes import Admin, signed_in_admin

_DISCORD = Settings(
    _env_file=None,  # pyright: ignore[reportCallIssue]
    database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
    turn=TurnSettings(path="mux", channel_backends=True),
    discord=DiscordSettings(bot_token=SecretStr("token")),
)


async def _named(
    factory: async_sessionmaker[AsyncSession], admin: Admin, names: dict[str, tuple[str, str]]
) -> None:
    """Discord people with a principal and a stored name and handle."""
    async with factory.begin() as db:
        for user_id in names:
            await get_or_create_platform_principal(
                db, tenant_id=admin.ws.tenant_id, platform="discord", external_id=user_id
            )
        await platform_names.upsert_user_names(
            db,
            tenant_id=admin.ws.tenant_id,
            platform="discord",
            names={u: KnownName(*n) for u, n in names.items()},
        )


async def _dm_agent(admin: Admin) -> str:
    [agent] = (await admin.client.get("/api/admin/dm")).json()["agents"]
    return agent["name"]


async def test_dm_default_and_rules_change_in_order_with_an_audit_row_each(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with signed_in_admin(web_client, db_session_factory, tmp_path) as admin:
        name = await _dm_agent(admin)
        api = admin.client
        default = await api.patch("/api/admin/dm", json={"default_agent": name})
        await api.post("/api/admin/dm/rules", json={"subject": "everyone", "agent": name})
        await api.post(
            "/api/admin/dm/rules",
            json={"subject": "group", "subject_id": "r-1", "agent": name, "locked": True},
        )
        await api.post(
            "/api/admin/dm/rules", json={"subject": "user", "subject_id": "u-1", "position": 0}
        )
        await api.patch("/api/admin/dm/rules/2", json={"locked": False})
        await api.patch("/api/admin/dm/rules/2", json={"to": 0})
        removed = await api.delete("/api/admin/dm/rules/1")
        trail = await admin.trail(db_session_factory)

    assert default.json()["default_agent"] == name, default.text
    assert [
        (r["position"], r["subject"], r["subject_id"], r["agent"], r["locked"])
        for r in removed.json()["rules"]
    ] == [(0, "group", "r-1", name, False), (1, "everyone", None, name, False)], (
        "rules keep the order they were moved to"
    )
    assert trail == [
        ("Changed DM default agent", "Direct messages", name),
        ("Added DM rule", "Direct messages", f"Everyone to {name}"),
        ("Added DM rule", "Direct messages", f"Group r-1 to {name}, locked"),
        ("Added DM rule", "Direct messages", "A person to the default agent"),
        ("Changed DM rule", "Direct messages", f"Group r-1 to {name}"),
        ("Moved DM rule", "Direct messages", "Group r-1: 3 to 1"),
        ("Removed DM rule", "Direct messages", "A person to the default agent"),
    ], "each change leaves one row, and a person is never named"


async def test_dm_writes_with_bad_input_are_refused_without_an_audit_row(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with signed_in_admin(web_client, db_session_factory, tmp_path) as admin:
        api = admin.client
        codes = {
            "unknown agent": (
                await api.patch("/api/admin/dm", json={"default_agent": "nobody"})
            ).status_code,
            "extra field": (
                await api.patch("/api/admin/dm", json={"default_agent": None, "x": 1})
            ).status_code,
            "lock without agent": (
                await api.post("/api/admin/dm/rules", json={"subject": "everyone", "locked": True})
            ).status_code,
            "user without id": (
                await api.post("/api/admin/dm/rules", json={"subject": "user"})
            ).status_code,
            "bad id": (
                await api.post(
                    "/api/admin/dm/rules", json={"subject": "group", "subject_id": "a b"}
                )
            ).status_code,
            "missing rule": (
                await api.patch("/api/admin/dm/rules/3", json={"locked": True})
            ).status_code,
            "missing rule removed": (await api.delete("/api/admin/dm/rules/0")).status_code,
            "bad grant": (
                await api.patch(
                    f"/api/admin/agent-access/{AGENT_ID}",
                    json={"dm": {"who": "listed", "user_ids": ["x" * 101]}},
                )
            ).status_code,
        }
        trail = await admin.trail(db_session_factory)

    assert codes == {
        "unknown agent": 422,
        "extra field": 422,
        "lock without agent": 422,
        "user without id": 422,
        "bad id": 422,
        "missing rule": 404,
        "missing rule removed": 404,
        "bad grant": 422,
    }, codes
    assert trail == [], "a refused change leaves no audit row"


async def test_dm_grant_on_agent_access_lists_names_and_audits_counts_once(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with signed_in_admin(web_client, db_session_factory, tmp_path) as admin:
        await _named(db_session_factory, admin, {"u-1": ("Bea", "bea")})
        [before] = (await admin.client.get("/api/admin/agent-access")).json()
        change = {"who": "listed", "user_ids": ["u-1", "u-2", "u-1"], "group_ids": ["r-1"]}
        changed = await admin.client.patch(
            f"/api/admin/agent-access/{AGENT_ID}", json={"dm": change}
        )
        again = await admin.client.patch(f"/api/admin/agent-access/{AGENT_ID}", json={"dm": change})
        [after] = (await admin.client.get("/api/admin/agent-access")).json()
        trail = await admin.trail(db_session_factory)

    assert before["dm"] == {"on": False, "who": "members", "users": [], "groups": []}, before
    assert changed.json()["dm"] == after["dm"], changed.text
    assert after["dm"] == {
        "on": False,
        "who": "listed",
        "users": [{"id": "u-1", "name": "Bea"}, {"id": "u-2", "name": None}],
        "groups": ["r-1"],
    }, "listed people show their stored name"
    assert again.status_code == 200, again.text
    assert after["web"] is True, "a DM change leaves web chat alone"
    assert trail == [("Changed DM access", DEFAULT_AGENT_NAME, "Listed: 2 people, 1 group")], (
        "one row for the change, none for the repeat"
    )


async def test_explain_reads_the_persons_roles_from_discord_and_names_the_rule(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The admin types only who; their roles come from Discord, as at delivery."""
    guild = "700000303"

    def discord(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(f"/api/v10/guilds/{guild}")
        if path in ("/members/11", "/members/12"):
            user = path.removeprefix("/members/")
            roles = ["21"] if user == "11" else []
            return httpx.Response(200, json={"user": {"id": user}, "roles": roles})
        if path == "":
            return httpx.Response(200, json={"id": guild, "owner_id": "1"})
        if path == "/roles":
            return httpx.Response(200, json=[{"id": r, "permissions": "0"} for r in (guild, "21")])
        return httpx.Response(500)

    async with signed_in_admin(
        web_client, db_session_factory, tmp_path, settings=_DISCORD, provider=FakeUpstream(discord)
    ) as admin:
        async with db_session_factory.begin() as db:
            await db.execute(
                update(Tenant).where(Tenant.id == admin.ws.tenant_id).values(external_id=guild)
            )
            await set_dm_enabled(db, tenant_id=admin.ws.tenant_id, enabled=True)
        name = await _dm_agent(admin)
        await admin.client.post(
            "/api/admin/dm/rules",
            json={"subject": "group", "subject_id": "21", "agent": name, "locked": True},
        )
        staff = await admin.client.get("/api/admin/dm/explain", params={"user": "11"})
        other = await admin.client.get("/api/admin/dm/explain", params={"user": "12"})
        unknown = await admin.client.get("/api/admin/dm/explain", params={"user": "13"})
        bad = await admin.client.get("/api/admin/dm/explain", params={"user": "a b"})

    assert staff.json() == {
        "route": "start",
        "agent": name,
        "reason": "locked",
        "agents": [],
        "allowed": [name],
        "rule": 0,
        "groups": ["21"],
    }, staff.text
    assert (other.json()["reason"], other.json()["rule"], other.json()["groups"]) == (
        "only",
        None,
        [],
    ), other.text
    assert (unknown.json()["route"], unknown.json()["groups"]) == ("unchecked", None), (
        "Discord could not say, so the lock cannot be checked"
    )
    assert bad.status_code == 422, "an id with a space is refused"


async def test_people_search_finds_stored_names_on_the_workspace_platform(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with signed_in_admin(web_client, db_session_factory, tmp_path) as admin:
        await _named(
            db_session_factory, admin, {"u-1": ("Bea Smith", "bea"), "u-2": ("Cal", "cal_100%")}
        )
        found = (await admin.client.get("/api/admin/dm/people", params={"q": "SMI"})).json()
        percent = (await admin.client.get("/api/admin/dm/people", params={"q": "%"})).json()
        empty = (await admin.client.get("/api/admin/dm/people", params={"q": " "})).json()

    assert found == [{"id": "u-1", "name": "Bea Smith"}], found
    assert [p["id"] for p in percent] == ["u-2"], "a wildcard is matched as text"
    assert empty == [], "a blank search finds no one"


async def test_platform_groups_list_live_roles_then_stored_ids_without_a_name(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    def roles(request: httpx.Request) -> httpx.Response:
        guild = request.url.path.split("/")[-2]
        return httpx.Response(
            200,
            json=[
                {"id": guild, "name": "@everyone", "position": 0},
                {"id": "r-1", "name": "Staff", "position": 2},
                {"id": "r-2", "name": "Bot", "position": 3, "managed": True},
                {"id": "r-3", "name": "Clients", "position": 1},
            ],
        )

    async with signed_in_admin(
        web_client,
        db_session_factory,
        tmp_path,
        settings=_DISCORD,
        provider=FakeUpstream(roles),
    ) as admin:
        async with db_session_factory.begin() as db:
            await dm_store.add_rule(
                db,
                tenant_id=admin.ws.tenant_id,
                subject="group",
                subject_id="r-9",
                agent_name=None,
                locked=False,
                set_by=None,
                position=None,
                now=datetime.now(UTC),
            )
        listed = (await admin.client.get("/api/admin/platform-groups")).json()

    assert listed == {
        "platform": "discord",
        "groups": [
            {"id": "r-1", "name": "Staff"},
            {"id": "r-3", "name": "Clients"},
            {"id": "r-9", "name": None},
        ],
        "problem": None,
    }, "live roles by rank, then a stored id the server no longer has"


async def test_platform_groups_say_unavailable_when_the_platform_fails(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with signed_in_admin(
        web_client,
        db_session_factory,
        tmp_path,
        settings=_DISCORD,
        provider=FakeUpstream(lambda _: httpx.Response(500)),
    ) as admin:
        listed = (await admin.client.get("/api/admin/platform-groups")).json()

    assert listed == {"platform": "discord", "groups": [], "problem": "unavailable"}, listed
