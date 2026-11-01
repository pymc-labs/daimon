"""Chat-platform sign-in and linking: installed workspaces only, platform ids, never email."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from daimon.core.config import Settings
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_roles as roles_store
from daimon.core.stores.domain import WebPersonRow
from daimon.core.stores.identity import (
    find_platform_principal,
    get_or_create_platform_principal,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import funded_workspace, shown_trail, signed_in, visitor_of
from .conftest import PUBLIC_URL, WebClientFactory, web_settings

_SESSION = "__Host-daimon_web"
_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}


def _settings() -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        hub={"discord_client_id": "dc", "discord_client_secret": "ds"},  # pyright: ignore[reportArgumentType]
    )


def _discord(user: dict[str, str], guilds: list[tuple[str, str]]) -> Any:
    """Discord's token, user and guild endpoints, answering as `user`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v10/oauth2/token":
            return httpx.Response(200, json={"access_token": "dtok", "token_type": "Bearer"})
        if request.url.path == "/api/v10/users/@me":
            return httpx.Response(200, json=user)
        if request.url.path == "/api/v10/users/@me/guilds":
            return httpx.Response(200, json=[{"id": g, "name": n} for g, n in guilds])
        return httpx.Response(404)

    return SimpleNamespace(handler=handler)


async def _walk(
    client: httpx.AsyncClient, *, link: bool = False
) -> tuple[httpx.Response, httpx.Response]:
    """Start a Discord sign-in (or link) and come back with a code, as a browser would."""
    if link:
        start = await client.post("/auth/discord/link", json={}, headers=_OWN)
        assert start.status_code == 200, start.text
        authorize = start.json()["url"]
    else:
        start = await client.get("/auth/discord/start")
        assert start.status_code == 303, start.text
        authorize = start.headers["location"]
    state = parse_qs(urlsplit(authorize).query)["state"][0]
    back = await client.get("/auth/discord/callback", params={"code": "c", "state": state})
    return start, back


def _error(response: httpx.Response) -> str | None:
    url = urlsplit(response.headers["location"])
    return parse_qs(url.query)["error"][0] if url.path == "/signin" else None


async def _web_member(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, email: str
) -> WebPersonRow:
    """A person who signed in with single sign-on: their own `web` principal."""
    person_id = uuid.uuid4()
    async with factory.begin() as db:
        principal = await get_or_create_platform_principal(
            db, tenant_id=tenant_id, platform="web", external_id=str(person_id)
        )
        return await people_store.upsert_member(
            db,
            tenant_id=tenant_id,
            account_id=principal.account_id,
            display_name="Ada",
            workspace_name="Acme",
            email=email,
            new_person_id=person_id,
        )


async def _people_of(factory: async_sessionmaker[AsyncSession], subject: str) -> list[WebPersonRow]:
    async with factory() as db:
        rows = await identities_store.find_identity(
            db, provider="discord", issuer="", subject=subject
        )
        return [
            p
            for row in rows
            if (
                p := await people_store.get_person(
                    db, tenant_id=row.tenant_id, person_id=row.person_id
                )
            )
        ]


async def test_discord_sign_in_reaches_only_workspaces_with_daimon_installed(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    installed = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    provider = _discord({"id": "u1", "username": "maya"}, [("g1", "Acme"), ("g2", "Other")])
    async with web_client(
        web_settings(providers="oidc,discord"),
        sessionmaker=db_session_factory,
        provider=provider,
        settings=_settings(),
    ) as client:
        listed = {p["id"] for p in (await client.get("/auth/providers")).json()}
        _, back = await _walk(client)
    people = await _people_of(db_session_factory, "u1")
    async with db_session_factory() as db:
        principal = await find_platform_principal(
            db, tenant_id=installed.id, platform="discord", external_id="u1"
        )
    assert "discord" in listed, f"a configured Discord app is offered: {listed}"
    assert back.status_code == 303 and _error(back) is None, back.headers
    assert [p.tenant_id for p in people] == [installed.id], "only the installed guild is reached"
    assert principal is not None and people[0].account_id == principal.account_id, (
        "the person runs as their Discord principal, the one the bot uses"
    )
    assert people[0].display_name == "maya" and people[0].workspace_name == "Acme", people[0]


async def test_discord_sign_in_with_no_installed_workspace_is_refused(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    provider = _discord({"id": "u1", "username": "maya"}, [("g-none", "Elsewhere")])
    async with web_client(
        web_settings(providers="discord"),
        sessionmaker=db_session_factory,
        provider=provider,
        settings=_settings(),
    ) as client:
        _, back = await _walk(client)
    assert _error(back) == "Daimon isn't in any of your workspaces yet. Ask an admin.", back.headers
    assert _SESSION not in back.cookies, "a refused sign-in opens no session"


async def test_email_never_links_a_chat_sign_in_to_an_existing_person(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    sso = await _web_member(db_session_factory, tenant.id, "ada@example.com")
    provider = _discord(
        {"id": "u1", "username": "ada", "email": "ada@example.com", "verified": "true"},
        [("g1", "Acme")],
    )
    async with web_client(
        web_settings(providers="discord"),
        sessionmaker=db_session_factory,
        provider=provider,
        settings=_settings(),
    ) as client:
        await _walk(client)
    people = await _people_of(db_session_factory, "u1")
    assert len(people) == 1 and people[0].id != sso.id, "the same email is a different person"
    assert people[0].account_id != sso.account_id, "and spends under a different account"


async def _link_rig(
    web_client: WebClientFactory,
    factory: async_sessionmaker[AsyncSession],
    person: WebPersonRow,
    user_id: str,
    run: Callable[[httpx.AsyncClient], Any],
) -> Any:
    token = await signed_in(factory, person)
    async with web_client(
        web_settings(providers="oidc,discord"),
        sessionmaker=factory,
        provider=_discord({"id": user_id, "username": "ada"}, [("g1", "Acme")]),
        settings=_settings(),
    ) as client:
        client.cookies.set(_SESSION, token, domain="web.example.com")
        return await run(client)


async def test_link_proves_a_new_discord_id_onto_the_persons_account(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    person = await _web_member(db_session_factory, tenant.id, "ada@example.com")
    _, back = await _link_rig(web_client, db_session_factory, person, "u7", _walk_link)
    async with db_session_factory() as db:
        principal = await find_platform_principal(
            db, tenant_id=tenant.id, platform="discord", external_id="u7"
        )
    assert _error(back) is None, back.headers
    assert principal is not None and principal.account_id == person.account_id, (
        "a Discord id Daimon never saw joins the signed-in person's account"
    )
    assert [p.id for p in await _people_of(db_session_factory, "u7")] == [person.id]


async def test_link_moves_a_person_without_history_onto_their_existing_discord_account(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    person = await _web_member(db_session_factory, tenant.id, "ada@example.com")
    async with db_session_factory.begin() as db:
        bot_known = await get_or_create_platform_principal(
            db, tenant_id=tenant.id, platform="discord", external_id="u7"
        )
        await roles_store.set_workspace_role(
            db,
            tenant_id=tenant.id,
            account_id=person.account_id,
            role="admin",
            granted_by_account_id=None,
        )
    await _link_rig(web_client, db_session_factory, person, "u7", _walk_link)
    async with db_session_factory() as db:
        moved = await people_store.get_person(db, tenant_id=tenant.id, person_id=person.id)
        carried = await roles_store.get_workspace_role(
            db, tenant_id=tenant.id, account_id=bot_known.account_id
        )
    trail = await shown_trail(db_session_factory, tenant.id)
    assert moved is not None and moved.account_id == bot_known.account_id, (
        "the person now spends as the account Discord already knew"
    )
    assert carried == "admin", "the role moved with them"
    assert sorted((a, d) for a, _, d in trail) == [
        ("Moved role to linked account", "Admin"),
        ("Switched account on link", "Discord"),
    ], "the role move and the account switch are audited"


async def test_link_is_refused_when_the_web_id_has_history(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    person = await _web_member(db_session_factory, tenant.id, "ada@example.com")
    async with db_session_factory.begin() as db:
        await get_or_create_platform_principal(
            db, tenant_id=tenant.id, platform="discord", external_id="u7"
        )
        await chat_store.create_thread(
            db,
            tenant_id=tenant.id,
            person_id=person.id,
            agent_id="ag",
            agent_name="A",
            title="t",
        )
    _, back = await _link_rig(web_client, db_session_factory, person, "u7", _walk_link)
    async with db_session_factory() as db:
        kept = await people_store.get_person(db, tenant_id=tenant.id, person_id=person.id)
    assert _error(back) == "This account already has its own history. Ask an admin.", back.headers
    assert kept is not None and kept.account_id == person.account_id, "nothing moved"


async def test_link_needs_a_signed_in_person(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with web_client(
        web_settings(providers="discord"),
        sessionmaker=db_session_factory,
        provider=_discord({"id": "u1", "username": "a"}, []),
        settings=_settings(),
    ) as client:
        res = await client.post("/auth/discord/link", json={}, headers=_OWN)
    assert res.status_code == 401, "linking starts from a signed-in session"


async def test_link_start_is_a_post_behind_the_csrf_check(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A cross-site page can neither POST the link start nor trigger it with a GET."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    person = await _web_member(db_session_factory, tenant.id, "ada@example.com")

    async def attempts(client: httpx.AsyncClient) -> tuple[httpx.Response, ...]:
        return (
            await client.post("/auth/discord/link", json={}),
            await client.post(
                "/auth/discord/link", json={}, headers={**_OWN, "Origin": "https://evil.example"}
            ),
            await client.get("/auth/discord/start", params={"link": "1"}),
        )

    refused_no_header, refused_origin, got = await _link_rig(
        web_client, db_session_factory, person, "u7", attempts
    )
    assert refused_no_header.status_code == 403, "the app's header is required"
    assert refused_origin.status_code == 403, "and so is our own origin"
    assert got.status_code == 303 and "link" not in got.headers["location"], (
        "the GET start no longer takes a link flag"
    )


async def test_a_get_start_never_links_even_with_the_old_flag(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    person = await _web_member(db_session_factory, tenant.id, "ada@example.com")

    async def old_style(client: httpx.AsyncClient) -> None:
        start = await client.get("/auth/discord/start", params={"link": "1"})
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        await client.get("/auth/discord/callback", params={"code": "c", "state": state})

    await _link_rig(web_client, db_session_factory, person, "u7", old_style)
    linked = await _people_of(db_session_factory, "u7")
    assert [p.id for p in linked] != [person.id], "a GET cannot attach a chat account to a person"


async def test_the_link_url_shows_discord_consent_but_sign_in_stays_silent(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    person = await _web_member(db_session_factory, tenant.id, "ada@example.com")

    async def both(client: httpx.AsyncClient) -> tuple[str, str]:
        linking = (await client.post("/auth/discord/link", json={}, headers=_OWN)).json()["url"]
        signing = (await client.get("/auth/discord/start")).headers["location"]
        return linking, signing

    linking, signing = await _link_rig(web_client, db_session_factory, person, "u7", both)
    assert "prompt" not in parse_qs(urlsplit(linking).query), "a link never skips consent"
    assert parse_qs(urlsplit(signing).query)["prompt"] == ["none"], "sign-in stays silent"


async def _walk_link(client: httpx.AsyncClient) -> tuple[httpx.Response, httpx.Response]:
    return await _walk(client, link=True)


async def test_microsoft_sign_in_reaches_the_teams_workspace_of_its_directory(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    directory = "11111111-1111-1111-1111-111111111111"
    oid = "22222222-2222-2222-2222-222222222222"
    teams = await make_tenant(db_session, platform="teams", workspace_id=directory)
    await db_session.commit()
    claims = {"tid": directory, "oid": oid.upper(), "aud": "mc", "name": "Ines"}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/{directory}/oauth2/v2.0/token", request.url
        return httpx.Response(200, json={"access_token": "at", "id_token": f"e30.{body}.s"})

    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        microsoft_accounts={  # pyright: ignore[reportArgumentType]
            "client_id": "mc",
            "client_secret": "ms",
            "tenant_id": directory,
            "authority": "https://login.example.com",
        },
    )
    async with web_client(
        web_settings(providers="microsoft"),
        sessionmaker=db_session_factory,
        provider=SimpleNamespace(handler=handler),
        settings=settings,
    ) as client:
        start = await client.get("/auth/microsoft/start")
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        back = await client.get("/auth/microsoft/callback", params={"code": "c", "state": state})
    async with db_session_factory() as db:
        principal = await find_platform_principal(
            db, tenant_id=teams.id, platform="teams", external_id=oid
        )
        people = await people_store.list_people(db, tenant_id=teams.id)
    assert _error(back) is None, back.headers
    assert principal is not None, "the account is the Teams person, by canonical object id"
    assert [p.account_id for p in people] == [principal.account_id], people


async def test_a_visitor_cannot_link_a_chat_account(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ws = await funded_workspace(db_session_factory)
    visitor = await visitor_of(db_session_factory, ws)

    async def start(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post("/auth/discord/link", json={}, headers=_OWN)

    res = await _link_rig(web_client, db_session_factory, visitor, "u7", start)

    assert res.status_code == 403, "a guest can't be moved onto a chat account"
    assert await _people_of(db_session_factory, "u7") == [], "nothing was linked"
