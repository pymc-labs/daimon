"""`/api/routines/*` as the web app calls it: rights, own-versus-all, and the service's checks."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from daimon.adapters.web import routines as routines_module
from daimon.adapters.web.channel_visibility import forget_visibility
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.config import DiscordSettings, Settings
from daimon.core.stores import accounts as accounts_store
from daimon.core.stores import platform_names
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_roles as roles_store_alias
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import WebPersonRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.routines import create_routine, list_routines_for_tenant
from daimon.testing.factories import make_platform_principal
from daimon.testing.turn_router import AGENT_ID, build_turn_router
from mux.testing.drivers.anthropic.ma_models import DEFAULT_AGENT_NAME
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import (
    Workspace,
    build_turns,
    funded_workspace,
    shown_trail,
    signed_in,
    visitor_of,
)
from .conftest import PUBLIC_URL, FakeUpstream, WebClientFactory, web_settings

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
_CHANNEL = "123456789012345678"
_NEW: dict[str, Any] = {
    "agent_id": AGENT_ID,
    "cron": "0 9 * * *",
    "timezone": "UTC",
    "message": "post the numbers",
}


async def _member(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    name: str,
    *,
    discord_id: str | None = None,
) -> WebPersonRow:
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


async def _seed(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, *, owner: str, message: str
) -> uuid.UUID:
    async with factory.begin() as session:
        row = await create_routine(
            session,
            tenant_id=tenant_id,
            created_by_user_id=owner,
            agent_id=AGENT_ID,
            agent_name=DEFAULT_AGENT_NAME,
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message=message,
            next_fire_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
    return row.id


class Rig:
    """A workspace with an owner and a member, talking to the app as either."""

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
        as_: str = "member",
        discord_id: str | None = "m-1",
        provider: FakeUpstream | None = None,
        settings: Settings | None = None,
    ) -> AsyncIterator[tuple[httpx.AsyncClient, Workspace, WebPersonRow]]:
        ws = await funded_workspace(self.factory)
        member = await _member(self.factory, ws.tenant_id, "Mia", discord_id=discord_id)
        async with self.factory.begin() as db:
            await roles_store_alias.set_workspace_role(
                db,
                tenant_id=ws.tenant_id,
                account_id=ws.person.account_id,
                role="owner",
                granted_by_account_id=None,
            )
        person = {"owner": ws.person, "member": member}.get(as_) or await visitor_of(
            self.factory, ws
        )
        turns = build_turns(self.factory, build_turn_router(str(ws.tenant_id)), self.tmp_path)
        token = await signed_in(self.factory, person)
        async with self.web_client(
            web_settings(),
            sessionmaker=self.factory,
            turns=turns,
            provider=provider,
            settings=settings,
        ) as client:
            client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
            client.headers.update(_OWN)
            yield client, ws, member


@pytest.fixture
def rig(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> Rig:
    return Rig(db_session_factory, tmp_path, web_client)


async def test_visitors_are_refused_every_route(rig: Rig) -> None:
    some = uuid.uuid4()
    routes = [
        ("GET", "/api/routines", None),
        ("GET", "/api/routines/choices", None),
        ("GET", f"/api/routines/{some}", None),
        ("POST", "/api/routines", _NEW),
        ("PATCH", f"/api/routines/{some}", {"message": "x"}),
        ("POST", f"/api/routines/{some}/pause", None),
        ("POST", f"/api/routines/{some}/resume", None),
        ("POST", f"/api/routines/{some}/run", None),
        ("DELETE", f"/api/routines/{some}", None),
    ]
    async with rig.open(as_="visitor") as (client, _, _):
        for method, path, body in routes:
            res = await client.request(method, path, json=body)
            assert res.status_code == 403, f"{method} {path} must be closed to visitors: {res.text}"


async def test_signed_out_callers_are_refused(rig: Rig) -> None:
    async with rig.open() as (client, _, _):
        client.cookies.clear()
        assert (await client.get("/api/routines")).status_code == 401


async def test_a_member_creates_lists_pauses_and_deletes_their_own_routine(rig: Rig) -> None:
    async with rig.open() as (client, ws, member):
        made = await client.post("/api/routines", json=_NEW)
        assert made.status_code == 200, made.text
        routine = made.json()
        assert routine["schedule"] == "every day at 09:00 UTC"
        assert routine["agent"] == {"id": AGENT_ID, "name": DEFAULT_AGENT_NAME}
        assert routine["mine"] is True
        assert routine["next_run"] is not None
        assert [r["id"] for r in (await client.get("/api/routines")).json()] == [routine["id"]]

        paused = (await client.post(f"/api/routines/{routine['id']}/pause")).json()
        assert (paused["enabled"], paused["next_run"]) == (False, None)
        resumed = (await client.post(f"/api/routines/{routine['id']}/resume")).json()
        assert resumed["enabled"] is True and resumed["next_run"] is not None

        edited = await client.patch(
            f"/api/routines/{routine['id']}", json={"message": "new words", "cron": "30 8 * * 1"}
        )
        assert edited.json()["message"] == "new words"
        assert edited.json()["schedule"] == "every Monday at 08:30 UTC"

        assert (await client.delete(f"/api/routines/{routine['id']}")).status_code == 204
        assert (await client.get("/api/routines")).json() == []
        trail = await shown_trail(rig.factory, ws.tenant_id)
    assert member.id != ws.person.id
    assert [action for action, _, _ in trail] == [
        "Created routine",
        "Paused routine",
        "Resumed routine",
        "Changed routine",
        "Deleted routine",
    ], "every change is recorded with the change"


async def test_a_routine_is_titled_by_its_name_else_by_the_start_of_its_prompt(rig: Rig) -> None:
    async with rig.open() as (client, _ws, _member):
        made = (await client.post("/api/routines", json=_NEW)).json()
        assert (made["name"], made["title"]) == (None, "post the numbers")

        named = (
            await client.patch(f"/api/routines/{made['id']}", json={"name": " Daily numbers "})
        ).json()
        assert (named["name"], named["title"]) == ("Daily numbers", "Daily numbers")

        cleared = (await client.patch(f"/api/routines/{made['id']}", json={"name": ""})).json()
        assert (cleared["name"], cleared["title"]) == (None, "post the numbers")

        spaced = (await client.post("/api/routines", json=_NEW | {"name": " Weekly "})).json()
        blank = (await client.post("/api/routines", json=_NEW | {"name": "   "})).json()
        assert (spaced["name"], blank["name"]) == ("Weekly", None), "create trims like update"


def test_the_title_falls_back_to_the_first_line_cut_at_sixty_characters() -> None:
    title = routines_module.routine_title
    assert title(None, "\n  first line\nsecond") == "first line"
    assert len(title(None, "x" * 200)) == 60, "a long first line is cut to 60 characters"
    assert title("Named", "ignored") == "Named"


async def test_run_now_makes_a_routine_due_is_audited_and_refused_while_paused(rig: Rig) -> None:
    async with rig.open() as (client, ws, _):
        routine = (await client.post("/api/routines", json=_NEW)).json()
        before = datetime.now(UTC)
        ran = await client.post(f"/api/routines/{routine['id']}/run")
        assert ran.status_code == 200, ran.text
        due = datetime.fromisoformat(ran.json()["next_run"])
        await client.post(f"/api/routines/{routine['id']}/pause")
        paused = await client.post(f"/api/routines/{routine['id']}/run")
        trail = await shown_trail(rig.factory, ws.tenant_id)

    assert before - timedelta(seconds=5) <= due <= datetime.now(UTC), "due as of now"
    assert (paused.status_code, paused.json()["detail"]) == (
        422,
        "This routine is paused. Resume it to run it.",
    )
    assert [action for action, _, _ in trail] == [
        "Created routine",
        "Ran routine now",
        "Paused routine",
    ], "a refused run leaves no row"


async def test_an_admin_runs_any_routine_now(rig: Rig) -> None:
    async with rig.open(as_="owner", discord_id=None) as (client, ws, _):
        theirs = await _seed(rig.factory, ws.tenant_id, owner="someone-else", message="theirs")
        ran = await client.post(f"/api/routines/{theirs}/run")

    assert ran.status_code == 200, ran.text
    assert ran.json()["mine"] is False


async def test_a_member_sees_and_changes_only_their_own_routines(rig: Rig) -> None:
    async with rig.open() as (client, ws, member):
        mine = await client.post("/api/routines", json=_NEW)
        theirs = await _seed(rig.factory, ws.tenant_id, owner="someone-else", message="private")
        listed = (await client.get("/api/routines")).json()
        assert [r["id"] for r in listed] == [mine.json()["id"]]
        for method, path, body in [
            ("GET", f"/api/routines/{theirs}", None),
            ("PATCH", f"/api/routines/{theirs}", {"message": "x"}),
            ("POST", f"/api/routines/{theirs}/pause", None),
            ("POST", f"/api/routines/{theirs}/run", None),
            ("DELETE", f"/api/routines/{theirs}", None),
        ]:
            res = await client.request(method, path, json=body)
            assert res.status_code == 404, f"{method} {path} must read as missing: {res.text}"
    assert member is not None


async def test_an_admin_sees_and_manages_every_routine(rig: Rig) -> None:
    async with rig.open(as_="owner", discord_id=None) as (client, ws, _):
        theirs = await _seed(rig.factory, ws.tenant_id, owner="someone-else", message="theirs")
        [row] = (await client.get("/api/routines")).json()
        assert row["mine"] is False
        paused = await client.post(f"/api/routines/{theirs}/pause")
        assert paused.json()["enabled"] is False
        assert (await client.delete(f"/api/routines/{theirs}")).status_code == 204
        async with rig.factory() as db:
            assert await list_routines_for_tenant(db, tenant_id=ws.tenant_id) == []


async def test_creating_needs_a_linked_chat_account(rig: Rig) -> None:
    async with rig.open(discord_id=None) as (client, _, _):
        choices = (await client.get("/api/routines/choices")).json()
        refused = await client.post("/api/routines", json=_NEW)
    assert choices["can_create"] is False
    assert refused.status_code == 409
    assert refused.json()["detail"] == "Link your Discord account to schedule routines."


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        ({"cron": "not a cron"}, "That schedule or time zone isn't valid."),
        ({"timezone": "Mars/Phobos"}, "That schedule or time zone isn't valid."),
        ({"agent_id": "agent_missing"}, "Pick an agent from the list."),
        ({"destination": "999"}, "Pick a channel from the list."),
    ],
)
async def test_bad_input_is_refused_in_plain_words(
    rig: Rig, change: dict[str, Any], detail: str
) -> None:
    async with rig.open() as (client, ws, _):
        res = await client.post("/api/routines", json=_NEW | change)
        async with rig.factory() as db:
            assert await list_routines_for_tenant(db, tenant_id=ws.tenant_id) == []
    assert (res.status_code, res.json()["detail"]) == (422, detail)


async def test_a_member_may_deliver_only_to_a_channel_their_platform_shows_them(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[str, frozenset[str]]] = []

    async def shown(
        _runtime: object, *, user_id: str | None, channel_ids: Collection[str], **_: object
    ) -> frozenset[str]:
        asked.append((str(user_id), frozenset(channel_ids)))
        return frozenset({_CHANNEL, "222", "333"} & set(channel_ids))

    monkeypatch.setattr(routines_module, "readable_channels", shown)
    async with rig.open() as (client, ws, _):
        async with rig.factory.begin() as db:
            await platform_names.upsert_channel_names(
                db,
                tenant_id=ws.tenant_id,
                platform="discord",
                names={_CHANNEL: "general", "222": "secret", "333": "board", "444": "hidden"},
            )
            await set_access_policy(
                db,
                tenant_id=ws.tenant_id,
                policy=TenantAccessPolicy(
                    channel_rules={"333": ChannelRule(readers="own", writers="own")}
                ),
            )
        choices = (await client.get("/api/routines/choices")).json()
        refused = await client.post("/api/routines", json=_NEW | {"destination": "444"})
        locked = await client.post("/api/routines", json=_NEW | {"destination": "333"})
        made = await client.post("/api/routines", json=_NEW | {"destination": _CHANNEL})
        cleared = await client.patch(
            f"/api/routines/{made.json()['id']}", json={"clear_destination": True}
        )
    assert choices["destinations"] == [
        {"id": _CHANNEL, "name": "general", "kind": "channel"},
        {"id": "222", "name": "secret", "kind": "channel"},
    ], "what the platform shows them, minus a channel kept to its own agents"
    assert {user for user, _ in asked} == {"m-1"}
    assert (refused.status_code, locked.status_code) == (422, 422)
    assert made.json()["destination"] == {"id": _CHANNEL, "name": "general", "kind": "channel"}
    assert cleared.json()["destination"] is None


async def test_a_member_delivers_by_direct_message_to_themselves_only(rig: Rig) -> None:
    async with rig.open(discord_id="222222222222222222") as (client, ws, _):
        choices = (await client.get("/api/routines/choices")).json()
        made = await client.post("/api/routines", json=_NEW | {"dm": True})
        assert made.status_code == 200, made.text
        cleared = await client.patch(
            f"/api/routines/{made.json()['id']}", json={"clear_destination": True}
        )
        back = await client.patch(f"/api/routines/{made.json()['id']}", json={"dm": True})
        async with rig.factory() as db:
            [row] = await list_routines_for_tenant(db, tenant_id=ws.tenant_id)
    assert choices["can_dm"] is True
    routine = made.json()
    assert (routine["dm"], routine["destination"], routine["sandbox_tools"]) == (True, None, False)
    assert cleared.json()["dm"] is False
    assert back.json()["dm"] is True
    assert (row.destination_kind, row.destination_id) == ("dm", "222222222222222222")


async def test_changing_a_routine_needs_a_linked_chat_account(rig: Rig) -> None:
    async with rig.open(as_="owner", discord_id=None) as (client, ws, _):
        theirs = await _seed(rig.factory, ws.tenant_id, owner="someone-else", message="theirs")
        choices = (await client.get("/api/routines/choices")).json()
        refused = await client.patch(f"/api/routines/{theirs}", json={"message": "mine now"})
    assert choices["can_dm"] is False
    assert refused.status_code == 409


_VIEW_CHANNEL = 1 << 10
_SEND_MESSAGES = 1 << 11


def _discord_guild(everyone_permissions: int) -> FakeUpstream:
    """A Discord server where `_CHANNEL` exists and @everyone holds `everyone_permissions`."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/members/m-1"):
            return httpx.Response(200, json={"roles": []})
        if path.endswith("/channels"):
            return httpx.Response(200, json=[{"id": _CHANNEL, "permission_overwrites": []}])
        guild_id = path.rsplit("/", 1)[-1]
        roles = [{"id": guild_id, "permissions": str(everyone_permissions)}]
        return httpx.Response(200, json={"owner_id": "someone-else", "roles": roles})

    return FakeUpstream(handler)


async def _create_routine_to_channel(rig: Rig, *, everyone_permissions: int) -> httpx.Response:
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        discord=DiscordSettings(bot_token=SecretStr("token")),
    )
    forget_visibility()
    async with rig.open(provider=_discord_guild(everyone_permissions), settings=settings) as (
        client,
        ws,
        _,
    ):
        async with rig.factory.begin() as db:
            await platform_names.upsert_channel_names(
                db, tenant_id=ws.tenant_id, platform="discord", names={_CHANNEL: "general"}
            )
        return await client.post("/api/routines", json=_NEW | {"destination": _CHANNEL})


async def test_a_member_may_deliver_to_a_channel_they_can_view_and_post_in(rig: Rig) -> None:
    res = await _create_routine_to_channel(rig, everyone_permissions=_VIEW_CHANNEL | _SEND_MESSAGES)
    assert res.status_code == 200, res.text


@pytest.mark.xfail(
    strict=True,
    reason="web routine destination check is looser than chat; feature-consistency",
)
async def test_a_member_may_not_deliver_to_a_channel_they_can_view_but_not_post_in(
    rig: Rig,
) -> None:
    """Chat refuses this destination (`_check_send_permission`); the web app must too."""
    res = await _create_routine_to_channel(rig, everyone_permissions=_VIEW_CHANNEL)
    assert res.status_code == 422, res.text
