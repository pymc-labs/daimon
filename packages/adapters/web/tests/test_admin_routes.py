"""`/api/admin/*` as the web app calls it: rights, shapes and an audit row per change."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
import httpx
import pytest
from daimon.adapters.web import directory
from daimon.core.access_policy import AgentRule, TenantAccessPolicy
from daimon.core.stores import web_audit
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_roles as roles_store
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import WebPersonRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.testing.turn_router import AGENT_ID, build_turn_router
from mux.testing.drivers.anthropic.ma_models import DEFAULT_AGENT_NAME
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .chat_support import Workspace, build_turns, funded_workspace, shown_trail, signed_in
from .conftest import PUBLIC_URL, WebClientFactory, web_settings

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
_COOKIE = "__Host-daimon_web"
_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/api/admin/people", None),
    ("GET", "/api/admin/groups", None),
    ("GET", "/api/admin/people/{member}", None),
    ("PATCH", "/api/admin/people/{member}", {"status": "suspended"}),
    ("POST", "/api/admin/people/{member}/link", None),
    ("DELETE", "/api/admin/people/{member}/link", None),
    ("GET", "/api/admin/invites", None),
    (
        "POST",
        "/api/admin/invites",
        {
            "email": "a@example.com",
            "role": "visitor",
            "agents": [AGENT_ID],
            "credit_usd": 5,
            "days": 7,
        },
    ),
    ("DELETE", f"/api/admin/invites/{uuid.uuid4()}", None),
    ("GET", "/api/admin/agent-access", None),
    ("PATCH", f"/api/admin/agent-access/{AGENT_ID}", {"web": False}),
    ("GET", "/api/admin/audit", None),
    ("GET", "/api/admin/settings", None),
    ("PATCH", "/api/admin/settings", {"modules": {"agents": False}}),
]


@dataclass
class Admin:
    client: httpx.AsyncClient
    ws: Workspace
    member: WebPersonRow
    member_token: str

    def as_member(self) -> None:
        self.client.cookies.set(_COOKIE, self.member_token, domain="web.example.com")

    async def trail(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> list[tuple[str, str, str | None]]:
        return await shown_trail(factory, self.ws.tenant_id)


async def _member(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, name: str = "Mia"
) -> WebPersonRow:
    person_id = uuid.uuid4()
    async with factory.begin() as db:
        principal = await get_or_create_platform_principal(
            db, tenant_id=tenant_id, platform="web", external_id=str(person_id)
        )
        return await people_store.upsert_member(
            db,
            tenant_id=tenant_id,
            account_id=principal.account_id,
            display_name=name,
            workspace_name="Acme",
            email=f"{name.lower()}@example.com",
            new_person_id=person_id,
        )


@asynccontextmanager
async def _admin(
    web_client: WebClientFactory, factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> AsyncIterator[Admin]:
    """Ada, the owner, signed in; Mia, a member, ready to sign in."""
    ws = await funded_workspace(factory)
    member = await _member(factory, ws.tenant_id)
    async with factory.begin() as db:
        await roles_store.set_workspace_role(
            db,
            tenant_id=ws.tenant_id,
            account_id=ws.person.account_id,
            role="owner",
            granted_by_account_id=None,
        )
    owner_token = await signed_in(factory, ws.person)
    member_token = await signed_in(factory, member)
    turns = build_turns(factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    async with web_client(web_settings(ws.tenant_id), sessionmaker=factory, turns=turns) as client:
        client.headers.update(_OWN)
        client.cookies.set(_COOKIE, owner_token, domain="web.example.com")
        yield Admin(client, ws, member, member_token)


async def test_members_are_refused_every_admin_route(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _admin(web_client, db_session_factory, tmp_path) as admin:
        admin.as_member()
        refused = {
            path: (
                await admin.client.request(method, path.format(member=admin.member.id), json=body)
            ).status_code
            for method, path, body in _ROUTES
        }

    assert set(refused.values()) == {403}, f"every admin route refuses a member: {refused}"
    assert await admin.trail(db_session_factory) == [], "a refused change leaves no audit row"


async def test_people_are_listed_and_role_and_status_changes_are_audited(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _admin(web_client, db_session_factory, tmp_path) as admin:
        listed = (await admin.client.get("/api/admin/people")).json()
        url = f"/api/admin/people/{admin.member.id}"
        promoted = await admin.client.patch(url, json={"role": "admin"})
        suspended = await admin.client.patch(url, json={"status": "suspended"})
        restored = await admin.client.patch(url, json={"status": "active"})
        demoted = await admin.client.patch(url, json={"role": "member"})
        myself = await admin.client.patch(
            f"/api/admin/people/{admin.ws.person.id}", json={"status": "suspended"}
        )
        sole_owner = await admin.client.patch(
            f"/api/admin/people/{admin.ws.person.id}", json={"role": "admin"}
        )

    mia = next(p for p in listed if p["id"] == str(admin.member.id))
    assert set(mia) == {
        "id",
        "name",
        "email",
        "role",
        "status",
        "groups",
        "identities",
        "credit",
        "last_seen",
    }, "a person has the shape the app reads"
    assert (mia["name"], mia["role"], mia["status"]) == ("Mia", "member", "active")
    assert promoted.json()["role"] == "admin", promoted.text
    assert suspended.json()["status"] == "suspended" and restored.json()["status"] == "active"
    assert demoted.json()["role"] == "member", demoted.text
    assert myself.status_code == 409, "an admin cannot suspend themselves"
    assert sole_owner.status_code == 409, "the last owner cannot step down"
    assert await admin.trail(db_session_factory) == [
        ("Changed role", "Mia", "Member to Admin"),
        ("Suspended", "Mia", None),
        ("Restored", "Mia", None),
        ("Changed role", "Mia", "Admin to Member"),
    ], "each change is audited once, and refused ones not at all"


async def test_groups_and_people_fill_the_pickers(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _admin(web_client, db_session_factory, tmp_path) as admin:
        async with db_session_factory.begin() as db:
            await identities_store.link_identity(
                db,
                tenant_id=admin.ws.tenant_id,
                person_id=admin.member.id,
                provider="oidc",
                issuer="https://id.example.com",
                subject="mia",
                label="mia",
                groups=["Leadership"],
            )
        groups = (await admin.client.get("/api/admin/groups")).json()
        found = (await admin.client.get("/api/admin/people", params={"q": "MIA"})).json()
        none = (await admin.client.get("/api/admin/people", params={"q": "nobody"})).json()

    assert groups == [
        {"id": "everyone", "name": "Everyone", "members": 2},
        {"id": "Leadership", "name": "Leadership", "members": 1},
    ], "the built-in group first, then each group people carry, with head counts"
    assert [p["id"] for p in found] == [str(admin.member.id)], "a name part finds the person"
    assert none == [], "no match is an empty list"


async def test_a_suspended_owner_does_not_count_as_the_workspace_owner(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _admin(web_client, db_session_factory, tmp_path) as admin:
        url = f"/api/admin/people/{admin.member.id}"
        await admin.client.patch(url, json={"role": "owner"})
        await admin.client.patch(url, json={"status": "suspended"})
        step_down = await admin.client.patch(
            f"/api/admin/people/{admin.ws.person.id}", json={"role": "admin"}
        )
        await admin.client.patch(url, json={"status": "active"})
        step_down_again = await admin.client.patch(
            f"/api/admin/people/{admin.ws.person.id}", json={"role": "admin"}
        )

    assert step_down.status_code == 409, "the only active owner cannot step down"
    assert step_down_again.status_code == 200, "with another active owner they can"


async def test_invites_are_created_listed_and_revoked(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    body = {
        "email": "guest@example.com",
        "role": "visitor",
        "agents": [AGENT_ID],
        "credit_usd": 10,
        "days": 7,
    }
    async with _admin(web_client, db_session_factory, tmp_path) as admin:
        made = await admin.client.post("/api/admin/invites", json=body)
        no_agent = await admin.client.post("/api/admin/invites", json={**body, "agents": []})
        listed = (await admin.client.get("/api/admin/invites")).json()
        revoked = await admin.client.delete(f"/api/admin/invites/{made.json()['id']}")
        after = (await admin.client.get("/api/admin/invites")).json()
        preview = await admin.client.get(f"/api/invites/{made.json()['link'].rsplit('/', 1)[1]}")

    invite = made.json()
    assert invite["link"].startswith(f"{PUBLIC_URL}/invite/"), "the link is shown once, on create"
    assert (invite["status"], invite["by"], invite["credit_usd"]) == ("pending", "Ada", 10.0)
    assert [a["name"] for a in invite["agents"]] == [DEFAULT_AGENT_NAME]
    assert no_agent.status_code == 422, "a visitor invite needs an agent"
    assert [i["id"] for i in listed] == [invite["id"]] and "link" not in listed[0], (
        "the list never shows a link again"
    )
    assert revoked.json() == {"ok": True} and after == [], "a revoked invite is gone"
    assert preview.status_code == 404, "and its link no longer opens"
    assert await admin.trail(db_session_factory) == [
        ("Created invite", "guest@example.com", f"Visitor, {DEFAULT_AGENT_NAME}, $10.00"),
        ("Revoked invite", "guest@example.com", None),
    ]


async def test_turning_web_chat_off_hides_the_agent_from_members(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _admin(web_client, db_session_factory, tmp_path) as admin:
        [before] = (await admin.client.get("/api/admin/agent-access")).json()
        changed = await admin.client.patch(
            f"/api/admin/agent-access/{AGENT_ID}", json={"web": False}
        )
        admin.as_member()
        agents = (await admin.client.get("/api/chat/agents")).json()

    assert before == {
        "agent": before["agent"],
        "web": True,
        "who": "everyone",
        "groups": [],
        "fixed_to": None,
    }, "an agent nobody configured is on for everyone"
    assert changed.json()["web"] is False, changed.text
    assert agents == [], "a member no longer sees it"
    assert await admin.trail(db_session_factory) == [
        ("Turned off web chat", DEFAULT_AGENT_NAME, None)
    ]


async def test_an_agent_kept_to_a_channel_cannot_be_opened_on_the_web(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _admin(web_client, db_session_factory, tmp_path) as admin:
        async with db_session_factory.begin() as db:
            await set_access_policy(
                db,
                tenant_id=admin.ws.tenant_id,
                policy=TenantAccessPolicy(
                    agent_rules={DEFAULT_AGENT_NAME: AgentRule(runs_in=("c-1",))}
                ),
            )
        [row] = (await admin.client.get("/api/admin/agent-access")).json()
        changed = await admin.client.patch(
            f"/api/admin/agent-access/{AGENT_ID}", json={"web": True}
        )

    assert (row["web"], row["fixed_to"]) == (False, "c-1"), "it shows where it is kept"
    assert changed.status_code == 403, "and its web switch cannot be turned on"
    assert await admin.trail(db_session_factory) == []


async def test_settings_are_read_and_changed_with_an_audit_row(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _admin(web_client, db_session_factory, tmp_path) as admin:
        before = (await admin.client.get("/api/admin/settings")).json()
        changed = await admin.client.patch(
            "/api/admin/settings", json={"modules": {"agents": False}}
        )
        lockout = await admin.client.patch("/api/admin/settings", json={"signin": {"oidc": False}})
        unknown = await admin.client.patch("/api/admin/settings", json={"signin": {"fax": True}})
        admin.as_member()
        me = (await admin.client.get("/api/me")).json()

    assert set(before) == {"modules", "signin", "default_agent", "apps"}, before
    assert before["modules"] == {"chat": True, "agents": True, "credits": True}
    assert [m["id"] for m in before["signin"]] == ["oidc"] and before["default_agent"] == AGENT_ID
    assert changed.json()["modules"]["agents"] is False, changed.text
    assert lockout.status_code == 422, "the last way to sign in stays on"
    assert unknown.status_code == 422, "only offered methods can be switched"
    assert "agents" not in me["modules"], "a module turned off is gone for the workspace"
    assert await admin.trail(db_session_factory) == [("Changed setting", "Modules", "Agents off")]


async def test_the_audit_log_names_who_acted(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _admin(web_client, db_session_factory, tmp_path) as admin:
        await admin.client.patch(
            f"/api/admin/people/{admin.member.id}", json={"status": "suspended"}
        )
        [entry] = (await admin.client.get("/api/admin/audit")).json()

    assert (entry["who"], entry["action"], entry["target"]) == ("Ada", "Suspended", "Mia")
    assert set(entry) == {"id", "at", "who", "action", "target"}, "no detail, no key"


async def test_the_audit_log_shows_an_erased_person_by_a_generic_label(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _admin(web_client, db_session_factory, tmp_path) as admin:
        url = f"/api/admin/people/{admin.member.id}"
        await admin.client.patch(url, json={"status": "suspended"})
        await admin.client.post(
            "/api/admin/invites",
            json={"email": "guest@example.com", "role": "member", "credit_usd": 0, "days": 7},
        )
        before = (await admin.client.get("/api/admin/audit")).json()
        async with db_session_factory.begin() as db:
            await people_store.delete_for_account(db, account_id=admin.member.account_id)
        after = (await admin.client.get("/api/admin/audit")).json()

    assert {e["action"]: e["target"] for e in before} == {
        "Suspended": "Mia",
        "Created invite": "guest@example.com",
    }, "while they exist, a person and an invite are shown by name and address"
    assert {e["action"]: e["target"] for e in after} == {
        "Suspended": "A person",
        "Created invite": "guest@example.com",
    }, "an erased person is a generic label; an invite nobody accepted is still its own"
    async with db_session_factory() as db:
        rows = await web_audit.list_recent(db, tenant_id=admin.ws.tenant_id)
    assert "Mia" not in " ".join(f"{r.target} {r.detail}" for r in rows), (
        "the stored text never held the name"
    )


async def _statements(engine: AsyncEngine, client: httpx.AsyncClient, path: str) -> int:
    """How many SQL statements one GET of `path` runs."""
    seen: list[str] = []

    def capture(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        response = await client.get(path)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    return len(seen)


async def test_the_people_list_runs_the_same_queries_however_many_people_there_are(
    web_client: WebClientFactory,
    db_engine: AsyncEngine,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _admin(web_client, db_session_factory, tmp_path) as admin:
        few = await _statements(db_engine, admin.client, "/api/admin/people")
        for name in ("Ben", "Cy", "Di", "Ed", "Flo"):
            await _member(db_session_factory, admin.ws.tenant_id, name)
        many = await _statements(db_engine, admin.client, "/api/admin/people")
        listed = (await admin.client.get("/api/admin/people")).json()

    assert len(listed) == 7, "everyone is listed"
    assert many == few, f"the list batches its loads: {few} statements for 2 people, {many} for 7"
    assert {p["credit"] for p in listed} == {None}, "a list leaves out each person's credit"


async def test_lists_page_with_a_cursor_and_stay_plain_arrays(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _admin(web_client, db_session_factory, tmp_path) as admin:
        for name in ("Ben", "Cy", "Di"):
            await _member(db_session_factory, admin.ws.tenant_id, name)
        for n in range(3):
            await admin.client.post(
                "/api/admin/invites",
                json={"email": f"g{n}@example.com", "role": "member", "credit_usd": 0, "days": 7},
            )
        seen: dict[str, list[str]] = {}
        for path, key in (("/api/admin/people", "id"), ("/api/admin/invites", "id")):
            ids: list[str] = []
            cursor: str | None = None
            while True:
                params: dict[str, Any] = {"limit": 2}
                if cursor:
                    params["cursor"] = cursor
                page = await admin.client.get(path, params=params)
                assert page.status_code == 200, page.text
                assert len(page.json()) <= 2, "a page holds at most `limit` rows"
                ids += [row[key] for row in page.json()]
                cursor = page.headers.get("X-Next-Cursor")
                if cursor is None:
                    break
            seen[path] = ids
        everyone = [p["id"] for p in (await admin.client.get("/api/admin/people")).json()]
        bad = await admin.client.get("/api/admin/people", params={"cursor": "nope"})
        huge = await admin.client.get("/api/admin/people", params={"limit": 100000})

    assert seen["/api/admin/people"] == everyone, "paging visits every person once, in order"
    assert len(seen["/api/admin/people"]) == 5, "the owner, Mia and three more"
    assert len(set(seen["/api/admin/invites"])) == len(seen["/api/admin/invites"]) == 3, (
        "paging visits every invite once"
    )
    assert bad.status_code == 422, "a made-up cursor is refused"
    assert huge.status_code == 422, "a limit past the cap is refused"


async def test_the_audit_log_pages_newest_first(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _admin(web_client, db_session_factory, tmp_path) as admin:
        url = f"/api/admin/people/{admin.member.id}"
        for status in ("suspended", "active", "suspended"):
            await admin.client.patch(url, json={"status": status})
        first = await admin.client.get("/api/admin/audit", params={"limit": 2})
        second = await admin.client.get(
            "/api/admin/audit",
            params={"limit": 2, "cursor": first.headers["X-Next-Cursor"]},
        )

    assert [e["action"] for e in first.json()] == ["Suspended", "Restored"], "newest first"
    assert [e["action"] for e in second.json()] == ["Suspended"], "the rest follow"
    assert "X-Next-Cursor" not in second.headers, "the last page names no next one"


async def test_an_id_in_the_path_that_is_no_uuid_is_not_found(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _admin(web_client, db_session_factory, tmp_path) as admin:
        refused = {
            (method, path): (await admin.client.request(method, path, json=body)).status_code
            for method, path, body in (
                ("GET", "/api/admin/people/nope", None),
                ("PATCH", "/api/admin/people/nope", {"status": "active"}),
                ("DELETE", "/api/admin/invites/nope", None),
                ("DELETE", "/api/credits/allowances/nope", None),
                ("GET", "/api/chat/sessions/nope", None),
            )
        }
        bad_body = await admin.client.patch(
            f"/api/admin/people/{admin.member.id}", json={"status": "gone"}
        )

    assert set(refused.values()) == {404}, f"a made-up id reads as an unknown one: {refused}"
    assert bad_body.status_code == 422, "other validation failures keep their 422"


async def test_an_unreachable_model_provider_is_a_503_not_a_500(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def down(*_: object, **__: object) -> list[Any]:
        raise anthropic.APIConnectionError(request=httpx.Request("GET", "https://api.example.com"))

    monkeypatch.setattr(directory, "list_agents_by_tenant", down)
    async with _admin(web_client, db_session_factory, tmp_path) as admin:
        got = {
            path: await admin.client.get(path)
            for path in ("/api/admin/agent-access", "/api/admin/settings", "/api/admin/audit")
        }

    assert got["/api/admin/agent-access"].status_code == 503, "agent access needs the provider"
    assert got["/api/admin/agent-access"].json() == {
        "detail": "The model provider is unreachable. Try again soon."
    }, "a short refusal the page can show"
    assert got["/api/admin/settings"].status_code == 503, "so does the settings list of agents"
    assert got["/api/admin/audit"].status_code == 200, "the log reads no agents"
