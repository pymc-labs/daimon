"""`/api/me`: the shape the single-page app reads, and the rights behind it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from daimon.adapters.web.cookies import new_session_token, token_sha256
from daimon.core.config import EmbeddedAppSettings, EmbeddedNavItem
from daimon.core.stores import accounts as accounts_store
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_roles as roles_store
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.stores import web_workspace_settings as workspace_settings
from daimon.core.stores.domain import Role as AccountRole
from daimon.core.stores.domain import WebPersonRow
from daimon.testing.factories import make_tenant, make_web_person
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import ISSUER, PUBLIC_URL, WebClientFactory, web_settings

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)

SignIn = Callable[[WebPersonRow], Awaitable[str]]


async def _sign_in_as(
    factory: async_sessionmaker[AsyncSession],
    person: WebPersonRow,
    *,
    groups: tuple[str, ...] = (),
    now: datetime = NOW,
    ttl: timedelta = timedelta(days=30),
) -> str:
    """Give `person` a live session directly and return its cookie value."""
    token = new_session_token()
    async with factory() as db, db.begin():
        await identities_store.link_identity(
            db,
            tenant_id=person.tenant_id,
            person_id=person.id,
            provider="oidc",
            issuer=ISSUER,
            subject=str(person.id),
            label="person@example.com",
            groups=groups,
        )
        await sessions_store.create_session(
            db,
            token_sha256=token_sha256(token),
            provider="oidc",
            issuer=ISSUER,
            subject=str(person.id),
            people=[person],
            ttl=ttl,
            now=now,
        )
    return token


async def _get_me(client: httpx.AsyncClient, token: str) -> httpx.Response:
    client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
    return await client.get("/api/me")


async def test_me_has_the_shape_the_app_reads(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="web")
    person = await make_web_person(db_session, tenant=tenant, email="ada@example.com")
    await db_session.commit()
    token = await _sign_in_as(db_session_factory, person)
    app = EmbeddedAppSettings(
        key="notes",
        name="Notes",
        icon="book",
        origin="https://notes.example.com",
        nav=(EmbeddedNavItem(label="Notes", path="/notes", icon="book"),),
        connections_path="/connections",
    )
    async with web_client(
        web_settings(person.tenant_id, apps=(app,)),
        sessionmaker=db_session_factory,
        now=lambda: NOW,
    ) as client:
        res = await _get_me(client, token)
    me = res.json()
    assert set(me) == {
        "id", "name", "email", "workspace", "workspaces", "role", "modules",
        "capabilities", "identities", "credit", "apps", "dev",
    }  # fmt: skip
    assert (me["id"], me["name"], me["email"]) == (str(person.id), "Test Person", "ada@example.com")
    assert me["workspace"] == {
        "id": str(person.tenant_id),
        "name": "Workspace",
        "platform": "web",
        "role": "member",
        "members": 1,
    }
    assert me["workspaces"] == [me["workspace"]]
    assert (me["role"], me["credit"], me["dev"]) == ("member", None, False)
    assert me["modules"] == ["chat", "agents", "credits"]
    assert me["capabilities"] == sorted(
        ["chat.use", "agents.view", "agents.edit", "agents.create", "credits.view_own"]
    )
    assert me["identities"] == [
        {"provider": "oidc", "provider_label": "Single sign-on", "label": "person@example.com"}
    ]
    assert me["apps"] == [
        {
            "key": "notes",
            "name": "Notes",
            "icon": "book",
            "origin": "https://notes.example.com",
            "nav": [{"label": "Notes", "path": "/notes", "icon": "book"}],
            "connections_path": "/connections",
        }
    ]


async def test_an_app_without_connections_reports_null(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await make_web_person(db_session)
    await db_session.commit()
    token = await _sign_in_as(db_session_factory, person)
    app = EmbeddedAppSettings(key="n", name="N", icon="book", origin="https://n.example.com")
    async with web_client(
        web_settings(person.tenant_id, apps=(app,)),
        sessionmaker=db_session_factory,
        now=lambda: NOW,
    ) as client:
        (shown,) = (await _get_me(client, token)).json()["apps"]
    assert shown["connections_path"] is None and shown["nav"] == [] and "panels" not in shown


async def test_identity_icon_comes_from_the_provider_settings(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await make_web_person(db_session)
    await db_session.commit()
    token = await _sign_in_as(db_session_factory, person)
    web = web_settings(person.tenant_id)
    assert web.oidc is not None
    web = web.model_copy(
        update={"oidc": web.oidc.model_copy(update={"label": "Example SSO", "icon": "key"})}
    )
    async with web_client(web, sessionmaker=db_session_factory, now=lambda: NOW) as client:
        (identity,) = (await _get_me(client, token)).json()["identities"]
    assert identity == {
        "provider": "oidc",
        "provider_label": "Example SSO",
        "label": "person@example.com",
        "icon": "key",
    }


async def test_roles_are_read_from_grants_held_now(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await make_web_person(db_session)
    admin = await make_web_person(db_session)
    by_group = await make_web_person(db_session)
    platform = await make_web_person(db_session)
    await db_session.commit()
    async with db_session_factory() as db, db.begin():
        await roles_store.set_workspace_role(
            db, tenant_id=owner.tenant_id, account_id=owner.account_id, role="owner",
            granted_by_account_id=None,
        )  # fmt: skip
        await roles_store.set_workspace_role(
            db, tenant_id=admin.tenant_id, account_id=admin.account_id, role="admin",
            granted_by_account_id=None,
        )  # fmt: skip
        await roles_store.set_group_role(
            db, tenant_id=by_group.tenant_id, issuer=ISSUER, group="ops", role="admin"
        )
        await accounts_store.set_role(db, platform.account_id, AccountRole.ADMIN)
    expected = {owner: "owner", admin: "admin", by_group: "admin", platform: "admin"}
    async with web_client(sessionmaker=db_session_factory, now=lambda: NOW) as client:
        for person, role in expected.items():
            groups = ("ops",) if person is by_group else ()
            token = await _sign_in_as(db_session_factory, person, groups=groups)
            me = (await _get_me(client, token)).json()
            got = (me["role"], me["workspace"]["role"], "people.manage" in me["capabilities"])
            assert got == (role, role, True), person.id
            assert me["modules"] == ["chat", "agents", "credits", "admin"]
        # Revoking the grant takes effect on the next request, not at next sign-in.
        async with db_session_factory() as db, db.begin():
            await db.execute(text("delete from workspace_admins"))
        token = await _sign_in_as(db_session_factory, owner)
        assert (await _get_me(client, token)).json()["role"] == "member"


async def test_no_session_is_401_and_a_forged_cookie_is_401(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with web_client(sessionmaker=db_session_factory) as client:
        none = await client.get("/api/me")
        client.cookies.set("__Host-daimon_web", "forged", domain="web.example.com")
        forged = await client.get("/api/me")
    assert (none.status_code, forged.status_code) == (401, 401)
    assert none.json() == {"detail": "Sign in to continue."}


async def test_an_expired_session_is_401(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await make_web_person(db_session)
    await db_session.commit()
    token = await _sign_in_as(db_session_factory, person, ttl=timedelta(days=30))
    async with web_client(
        web_settings(person.tenant_id),
        sessionmaker=db_session_factory,
        now=lambda: NOW + timedelta(days=31),
    ) as client:
        assert (await _get_me(client, token)).status_code == 401


async def test_a_suspended_person_is_403(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await make_web_person(db_session)
    await db_session.commit()
    token = await _sign_in_as(db_session_factory, person)
    async with db_session_factory() as db, db.begin():
        await people_store.update_person(db, person_id=person.id, status="suspended")
    async with web_client(sessionmaker=db_session_factory, now=lambda: NOW) as client:
        res = await _get_me(client, token)
    assert res.status_code == 403 and res.json() == {"detail": "This account is suspended."}


async def test_visits_never_extend_the_session_past_its_lifetime(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await make_web_person(db_session)
    await db_session.commit()
    token = await _sign_in_as(db_session_factory, person, ttl=timedelta(days=30))
    visit = NOW + timedelta(days=20)
    async with web_client(
        web_settings(person.tenant_id), sessionmaker=db_session_factory, now=lambda: visit
    ) as client:
        res = await _get_me(client, token)
    async with db_session_factory() as db:
        live = await sessions_store.get_live_session(
            db, token_sha256=token_sha256(token), now=NOW + timedelta(days=25)
        )
        after = await sessions_store.get_live_session(
            db, token_sha256=token_sha256(token), now=NOW + timedelta(days=31)
        )
    assert res.status_code == 200, "a visit inside the lifetime works"
    assert "set-cookie" not in res.headers, "the cookie is not re-issued by a visit"
    assert live is not None and live.last_seen_at == visit, "the visit is recorded"
    assert after is None, "the session still ends 30 days after sign-in"


async def test_a_session_older_than_the_current_lifetime_is_ended(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Lowering `session_ttl_days` applies to sign-ins already open."""
    person = await make_web_person(db_session)
    await db_session.commit()
    token = await _sign_in_as(db_session_factory, person, ttl=timedelta(days=30))
    async with web_client(
        web_settings(person.tenant_id, session_ttl_days=1),
        sessionmaker=db_session_factory,
        now=lambda: NOW + timedelta(days=2),
    ) as client:
        res = await _get_me(client, token)
    async with db_session_factory() as db:
        left = await sessions_store.get_live_session(
            db, token_sha256=token_sha256(token), now=NOW + timedelta(days=2)
        )
    assert res.status_code == 401, "the shorter lifetime has passed"
    assert left is None, "and the row is deleted"


async def _me_after(
    web_client: WebClientFactory,
    factory: async_sessionmaker[AsyncSession],
    person: WebPersonRow,
    token: str,
    *,
    minutes: int,
) -> httpx.Response:
    async with web_client(
        web_settings(person.tenant_id),
        sessionmaker=factory,
        now=lambda: NOW + timedelta(minutes=minutes),
    ) as client:
        return await _get_me(client, token)


async def test_turning_the_sign_in_method_off_ends_open_sessions_at_the_next_recheck(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await make_web_person(db_session)
    await db_session.commit()
    token = await _sign_in_as(db_session_factory, person)
    async with db_session_factory.begin() as db:
        await workspace_settings.save_settings(
            db, tenant_id=person.tenant_id, modules_off=[], signin_off=["oidc"],
            default_agent=None, now=NOW,
        )  # fmt: skip

    early = await _me_after(web_client, db_session_factory, person, token, minutes=5)
    due = await _me_after(web_client, db_session_factory, person, token, minutes=16)
    again = await _me_after(web_client, db_session_factory, person, token, minutes=17)

    assert early.status_code == 200, "before the recheck is due nothing is looked up"
    assert due.status_code == 401, "the recheck finds the method off"
    assert again.status_code == 401, "and the session is gone, not just refused once"
    async with db_session_factory() as db:
        left = await sessions_store.get_live_session(db, token_sha256=token_sha256(token), now=NOW)
    assert left is None, "the session row is deleted"


async def test_archiving_the_workspace_ends_open_sessions_at_the_next_recheck(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await make_web_person(db_session)
    await db_session.commit()
    token = await _sign_in_as(db_session_factory, person)
    await tenants_store.set_provision_status(
        db_session_factory, tenant_id=person.tenant_id, archive=True
    )

    due = await _me_after(web_client, db_session_factory, person, token, minutes=16)

    assert due.status_code == 401, "an archived workspace no longer honours its sign-ins"


async def test_a_passing_recheck_is_recorded_and_not_repeated_for_a_while(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await make_web_person(db_session)
    await db_session.commit()
    token = await _sign_in_as(db_session_factory, person)

    first = await _me_after(web_client, db_session_factory, person, token, minutes=16)
    async with db_session_factory() as db:
        live = await sessions_store.get_live_session(db, token_sha256=token_sha256(token), now=NOW)
    assert first.status_code == 200, "an allowed sign-in keeps working"
    assert live is not None and live.roles_checked_at == NOW + timedelta(minutes=16), (
        "the passing check is recorded"
    )


async def test_switching_to_a_workspace_you_are_not_in_is_404(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await make_web_person(db_session)
    other = await make_tenant(db_session, workspace_id="elsewhere")
    await db_session.commit()
    token = await _sign_in_as(db_session_factory, person)
    async with web_client(sessionmaker=db_session_factory, now=lambda: NOW) as client:
        client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
        foreign = await client.post(
            "/api/workspaces/switch", json={"id": str(other.id)}, headers=_OWN
        )
        junk = await client.post("/api/workspaces/switch", json={"id": "not-a-uuid"}, headers=_OWN)
    assert (foreign.status_code, junk.status_code) == (404, 404)


async def _two_workspace_session(
    factory: async_sessionmaker[AsyncSession], first: WebPersonRow, second: WebPersonRow
) -> str:
    token = new_session_token()
    async with factory.begin() as db:
        await sessions_store.create_session(
            db,
            token_sha256=token_sha256(token),
            provider="oidc",
            issuer=ISSUER,
            subject="sub",
            people=[first, second],
            ttl=timedelta(days=30),
            now=NOW,
        )
    return token


async def test_a_suspended_person_can_still_switch_to_another_workspace(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await make_web_person(db_session)
    second = await make_web_person(db_session)
    await db_session.commit()
    token = await _two_workspace_session(db_session_factory, first, second)
    async with db_session_factory.begin() as db:
        await people_store.update_person(db, person_id=first.id, status="suspended")
    async with web_client(sessionmaker=db_session_factory, now=lambda: NOW) as client:
        client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
        locked = await client.get("/api/me")
        switched = await client.post(
            "/api/workspaces/switch", json={"id": str(second.tenant_id)}, headers=_OWN
        )
        me = await client.get("/api/me")

    assert locked.status_code == 403, "the suspended workspace itself stays closed"
    assert switched.status_code == 200, "but the person can leave it for another"
    assert me.json()["workspace"]["id"] == str(second.tenant_id), "and is working there"


async def test_switching_to_a_workspace_that_turned_the_sign_in_off_is_404(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await make_web_person(db_session)
    second = await make_web_person(db_session)
    await db_session.commit()
    token = await _two_workspace_session(db_session_factory, first, second)
    async with db_session_factory.begin() as db:
        await workspace_settings.save_settings(
            db, tenant_id=second.tenant_id, modules_off=[], signin_off=["oidc"],
            default_agent=None, now=NOW,
        )  # fmt: skip
    async with web_client(sessionmaker=db_session_factory, now=lambda: NOW) as client:
        client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
        refused = await client.post(
            "/api/workspaces/switch", json={"id": str(second.tenant_id)}, headers=_OWN
        )
        me = await client.get("/api/me")

    assert refused.status_code == 404, "a workspace that refuses this sign-in cannot be entered"
    assert me.json()["workspace"]["id"] == str(first.tenant_id), "the session stays where it was"


async def test_switching_without_a_session_is_401(web_client: WebClientFactory) -> None:
    async with web_client() as client:
        res = await client.post("/api/workspaces/switch", json={"id": "x"}, headers=_OWN)
    assert res.status_code == 401


async def test_me_lists_every_workspace_with_its_role_and_member_count(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await make_web_person(db_session)
    second_tenant = await make_tenant(db_session)
    second = await make_web_person(db_session, tenant=second_tenant)
    await make_web_person(db_session, tenant=second_tenant, email="b@example.com")
    await db_session.commit()
    token = await _two_workspace_session(db_session_factory, first, second)
    async with db_session_factory.begin() as db:
        await roles_store.set_workspace_role(
            db,
            tenant_id=second.tenant_id,
            account_id=second.account_id,
            role="admin",
            granted_by_account_id=None,
        )
    async with web_client(sessionmaker=db_session_factory, now=lambda: NOW) as client:
        client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
        me = (await client.get("/api/me")).json()
        meter = await client.get("/api/credits/meter")

    seen = {w["id"]: (w["role"], w["members"]) for w in me["workspaces"]}
    assert seen == {
        str(first.tenant_id): ("member", 1),
        str(second.tenant_id): ("admin", 2),
    }, "each workspace carries the person's own role and its own head count"
    assert meter.status_code == 200 and meter.json() == me["credit"], (
        "the meter alone says what `credit` does"
    )


async def test_me_names_the_oidc_workspace_from_settings_and_others_from_their_stored_name(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The sign-in provider's configured name wins for its own workspace, however stale the stored one."""
    first = await make_web_person(db_session)
    second_tenant = await make_tenant(db_session)
    second = await make_web_person(db_session, tenant=second_tenant)
    await db_session.commit()
    token = await _two_workspace_session(db_session_factory, first, second)
    web = web_settings(first.tenant_id)
    assert web.oidc is not None, "the helper configures sign-in"
    web = web.model_copy(
        update={"oidc": web.oidc.model_copy(update={"workspace_name": "Fernhill"})}
    )
    async with web_client(web, sessionmaker=db_session_factory, now=lambda: NOW) as client:
        client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
        me = (await client.get("/api/me")).json()

    names = {w["id"]: w["name"] for w in me["workspaces"]}
    assert names == {
        str(first.tenant_id): "Fernhill",
        str(second.tenant_id): "Test Workspace",
    }, "the OIDC workspace reads the setting; another shows the name stored with the person"
