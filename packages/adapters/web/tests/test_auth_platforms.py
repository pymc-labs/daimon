"""Chat-platform sign-in and linking: installed workspaces only, platform ids, never email."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
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
from .conftest import (
    MICROSOFT_APP,
    MICROSOFT_TENANT,
    PUBLIC_URL,
    FakeUpstream,
    WebClientFactory,
    chat_settings,
    discord_settings,
    fake_discord,
    fake_microsoft,
    fake_slack,
    web_settings,
)

_SESSION = "__Host-daimon_web"
_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}


async def _walk(
    client: httpx.AsyncClient, *, connect: bool = False, provider: str = "discord"
) -> tuple[httpx.Response, httpx.Response]:
    """Start a sign-in (or connect) and come back with a code, as a browser would."""
    if connect:
        start = await client.post(f"/auth/{provider}/connect", json={}, headers=_OWN)
        assert start.status_code == 200, start.text
        authorize = start.json()["url"]
    else:
        start = await client.get(f"/auth/{provider}/start")
        assert start.status_code == 303, start.text
        authorize = start.headers["location"]
    state = parse_qs(urlsplit(authorize).query)["state"][0]
    back = await client.get(f"/auth/{provider}/callback", params={"code": "c", "state": state})
    return start, back


def _error(response: httpx.Response) -> str | None:
    url = urlsplit(response.headers["location"])
    return parse_qs(url.query)["error"][0] if url.path == "/signin" else None


async def _web_member(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, email: str
) -> WebPersonRow:
    """A person on a `web` principal of their own, not a chat one."""
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


async def _people_of(
    factory: async_sessionmaker[AsyncSession], subject: str, provider: str = "discord"
) -> list[WebPersonRow]:
    async with factory() as db:
        rows = await identities_store.find_identity(
            db, provider=provider, issuer="", subject=subject
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
    provider = fake_discord({"id": "u1", "username": "maya"}, [("g1", "Acme"), ("g2", "Other")])
    async with web_client(
        web_settings(),
        sessionmaker=db_session_factory,
        provider=provider,
        settings=discord_settings(),
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
    provider = fake_discord({"id": "u1", "username": "maya"}, [("g-none", "Elsewhere")])
    async with web_client(
        web_settings(),
        sessionmaker=db_session_factory,
        provider=provider,
        settings=discord_settings(),
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
    provider = fake_discord(
        {"id": "u1", "username": "ada", "email": "ada@example.com", "verified": "true"},
        [("g1", "Acme")],
    )
    async with web_client(
        web_settings(),
        sessionmaker=db_session_factory,
        provider=provider,
        settings=discord_settings(),
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
    *,
    upstream: FakeUpstream | None = None,
) -> Any:
    token = await signed_in(factory, person, identity=False)
    async with web_client(
        web_settings(),
        sessionmaker=factory,
        provider=upstream or fake_discord({"id": user_id, "username": "ada"}, [("g1", "Acme")]),
        settings=chat_settings(),
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
        ("Connected account", "Discord"),
        ("Moved role to linked account", "Admin"),
        ("Switched account on link", "Discord"),
    ], "the connect, the role move and the account switch are audited"


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
        web_settings(),
        sessionmaker=db_session_factory,
        provider=fake_discord({"id": "u1", "username": "a"}, []),
        settings=discord_settings(),
    ) as client:
        res = await client.post("/auth/discord/connect", json={}, headers=_OWN)
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
            await client.post("/auth/discord/connect", json={}),
            await client.post(
                "/auth/discord/connect", json={}, headers={**_OWN, "Origin": "https://evil.example"}
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
        linking = (await client.post("/auth/discord/connect", json={}, headers=_OWN)).json()["url"]
        signing = (await client.get("/auth/discord/start")).headers["location"]
        return linking, signing

    linking, signing = await _link_rig(web_client, db_session_factory, person, "u7", both)
    assert "prompt" not in parse_qs(urlsplit(linking).query), "a link never skips consent"
    assert parse_qs(urlsplit(signing).query)["prompt"] == ["none"], "sign-in stays silent"


async def _walk_link(client: httpx.AsyncClient) -> tuple[httpx.Response, httpx.Response]:
    return await _walk(client, connect=True)


async def test_a_visitor_cannot_link_a_chat_account(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ws = await funded_workspace(db_session_factory)
    visitor = await visitor_of(db_session_factory, ws)

    async def start(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post("/auth/discord/connect", json={}, headers=_OWN)

    res = await _link_rig(web_client, db_session_factory, visitor, "u7", start)

    assert res.status_code == 403, "a guest can't be moved onto a chat account"
    assert await _people_of(db_session_factory, "u7") == [], "nothing was linked"


async def _chat_rig(
    web_client: WebClientFactory,
    factory: async_sessionmaker[AsyncSession],
    upstream: FakeUpstream,
    run: Callable[[httpx.AsyncClient], Any],
) -> Any:
    async with web_client(
        web_settings(), sessionmaker=factory, provider=upstream, settings=chat_settings()
    ) as client:
        return await run(client)


async def test_every_configured_chat_platform_is_offered(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async def listed(client: httpx.AsyncClient) -> list[dict[str, Any]]:
        return (await client.get("/auth/providers")).json()

    providers = await _chat_rig(web_client, db_session_factory, fake_slack(team=None), listed)
    assert [(p["id"], p["label"], p["kind"]) for p in providers] == [
        ("discord", "Discord", "redirect"),
        ("slack", "Slack", "redirect"),
        ("microsoft", "Microsoft", "redirect"),
    ], providers


async def test_slack_authorize_url_asks_for_the_user_grant_without_pkce(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async def start(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get("/auth/slack/start")

    res: httpx.Response = await _chat_rig(
        web_client, db_session_factory, fake_slack(team=None), start
    )
    url = urlsplit(res.headers["location"])
    query = parse_qs(url.query)
    assert (url.netloc, url.path) == ("slack.com", "/oauth/v2/authorize"), url
    assert query["user_scope"] == ["users:read"] and "scope" not in query, (
        "a user grant only: `scope` would install the bot"
    )
    assert query["redirect_uri"] == [f"{PUBLIC_URL}/auth/slack/callback"], query
    assert "code_challenge" not in query, "Slack takes no PKCE"


async def test_slack_sign_in_reaches_only_the_installed_workspace_it_was_given_in(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    installed = await make_tenant(db_session, platform="slack", workspace_id="T1")
    await db_session.commit()

    async def walk(client: httpx.AsyncClient) -> httpx.Response:
        return (await _walk(client, provider="slack"))[1]

    back = await _chat_rig(
        web_client, db_session_factory, fake_slack(team=("T1", "Acme"), user_id="U1"), walk
    )
    people = await _people_of(db_session_factory, "T1:U1", "slack")
    async with db_session_factory() as db:
        principal = await find_platform_principal(
            db, tenant_id=installed.id, platform="slack", external_id="U1"
        )
    assert _error(back) is None, back.headers
    assert [p.tenant_id for p in people] == [installed.id], "the one workspace the grant names"
    assert principal is not None and people[0].account_id == principal.account_id, (
        "the person runs as their Slack user id, the principal the bot uses"
    )
    assert people[0].display_name == "ada" and people[0].workspace_name == "Acme", people[0]


async def test_slack_sign_in_to_a_workspace_without_daimon_is_refused(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await make_tenant(db_session, platform="slack", workspace_id="T1")
    await db_session.commit()

    async def walk(client: httpx.AsyncClient) -> httpx.Response:
        return (await _walk(client, provider="slack"))[1]

    back = await _chat_rig(
        web_client, db_session_factory, fake_slack(team=("T9", "Elsewhere")), walk
    )
    assert _error(back) == "Daimon isn't in any of your workspaces yet. Ask an admin.", back.headers
    assert _SESSION not in back.cookies, "a refused sign-in opens no session"


async def test_slack_grant_without_a_team_is_refused(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await make_tenant(db_session, platform="slack", workspace_id="T1")
    await db_session.commit()

    async def walk(client: httpx.AsyncClient) -> httpx.Response:
        return (await _walk(client, provider="slack"))[1]

    back = await _chat_rig(web_client, db_session_factory, fake_slack(team=None), walk)
    assert _error(back) == "Sign-in didn't work. Try again.", back.headers
    assert _SESSION not in back.cookies, "no workspace proven, no session"
    assert await _people_of(db_session_factory, "T1:U1", "slack") == [], "nobody was created"


async def test_microsoft_sign_in_reaches_the_teams_workspace_of_its_directory(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    oid = "22222222-2222-2222-2222-222222222222"
    teams = await make_tenant(db_session, platform="teams", workspace_id=MICROSOFT_TENANT)
    await db_session.commit()
    upstream = fake_microsoft(tid=MICROSOFT_TENANT, oid=oid.upper(), aud=MICROSOFT_APP, name="Ines")

    async def walk(client: httpx.AsyncClient) -> httpx.Response:
        return (await _walk(client, provider="microsoft"))[1]

    back = await _chat_rig(web_client, db_session_factory, upstream, walk)
    async with db_session_factory() as db:
        principal = await find_platform_principal(
            db, tenant_id=teams.id, platform="teams", external_id=oid
        )
        people = await people_store.list_people(db, tenant_id=teams.id)
    assert _error(back) is None, back.headers
    assert principal is not None, "the account is the Teams person, by canonical object id"
    assert [p.account_id for p in people] == [principal.account_id], people
    assert people[0].display_name == "Ines", people[0]


async def test_microsoft_sign_in_from_another_directory_is_refused(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams = await make_tenant(db_session, platform="teams", workspace_id=MICROSOFT_TENANT)
    await db_session.commit()
    upstream = fake_microsoft(
        tid="33333333-3333-3333-3333-333333333333",
        oid="22222222-2222-2222-2222-222222222222",
        aud=MICROSOFT_APP,
    )

    async def walk(client: httpx.AsyncClient) -> httpx.Response:
        return (await _walk(client, provider="microsoft"))[1]

    back = await _chat_rig(web_client, db_session_factory, upstream, walk)
    async with db_session_factory() as db:
        people = await people_store.list_people(db, tenant_id=teams.id)
    assert _error(back) == "Sign-in didn't work. Try again.", back.headers
    assert _SESSION not in back.cookies and people == [], "a foreign tenant reaches nothing"


async def test_microsoft_link_asks_to_pick_an_account_but_sign_in_does_not(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    person = await _web_member(db_session_factory, tenant.id, "ada@example.com")

    async def both(client: httpx.AsyncClient) -> tuple[str, str]:
        linking = (await client.post("/auth/microsoft/connect", json={}, headers=_OWN)).json()[
            "url"
        ]
        signing = (await client.get("/auth/microsoft/start")).headers["location"]
        return linking, signing

    urls: tuple[str, str] = await _link_rig(
        web_client, db_session_factory, person, "u7", both, upstream=fake_microsoft()
    )
    link_query, sign_query = (parse_qs(urlsplit(u).query) for u in urls)
    assert link_query["prompt"] == ["select_account"], "a link lets the person pick the account"
    assert "prompt" not in sign_query, "sign-in takes the account the browser has"
    assert link_query["scope"] == ["openid profile"], "identity only, no email or Graph access"
    assert link_query["code_challenge_method"] == ["S256"], link_query


async def test_link_proves_a_slack_user_onto_the_persons_account(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T1")
    await db_session.commit()
    person = await _web_member(db_session_factory, tenant.id, "ada@example.com")

    async def walk(client: httpx.AsyncClient) -> httpx.Response:
        return (await _walk(client, connect=True, provider="slack"))[1]

    back = await _link_rig(
        web_client,
        db_session_factory,
        person,
        "U7",
        walk,
        upstream=fake_slack(team=("T1", "Acme"), user_id="U7"),
    )
    async with db_session_factory() as db:
        principal = await find_platform_principal(
            db, tenant_id=tenant.id, platform="slack", external_id="U7"
        )
    assert _error(back) is None, back.headers
    assert principal is not None and principal.account_id == person.account_id, (
        "a Slack user id Daimon never saw joins the signed-in person's account"
    )
    assert [p.id for p in await _people_of(db_session_factory, "T1:U7", "slack")] == [person.id]


async def test_connecting_another_platform_never_links_it_onto_the_person(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    discord = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await make_tenant(db_session, platform="slack", workspace_id="T1")
    await db_session.commit()
    person = await _web_member(db_session_factory, discord.id, "ada@example.com")

    async def walk(client: httpx.AsyncClient) -> httpx.Response:
        return (await _walk(client, connect=True, provider="slack"))[1]

    back = await _link_rig(
        web_client,
        db_session_factory,
        person,
        "U7",
        walk,
        upstream=fake_slack(team=("T1", "Acme"), user_id="U7"),
    )
    slack_people = await _people_of(db_session_factory, "T1:U7", "slack")
    assert _error(back) is None, back.headers
    assert [p.id for p in slack_people] != [person.id] and len(slack_people) == 1, (
        "the Slack account stays its own person in its own workspace, not the Discord person"
    )


async def test_link_is_refused_for_a_slack_user_already_linked_to_someone_else(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T1")
    await db_session.commit()
    ada = await _web_member(db_session_factory, tenant.id, "ada@example.com")
    bea = await _web_member(db_session_factory, tenant.id, "bea@example.com")

    async def walk(client: httpx.AsyncClient) -> httpx.Response:
        return (await _walk(client, connect=True, provider="slack"))[1]

    upstream = fake_slack(team=("T1", "Acme"), user_id="U7")
    first = await _link_rig(web_client, db_session_factory, ada, "U7", walk, upstream=upstream)
    second = await _link_rig(web_client, db_session_factory, bea, "U7", walk, upstream=upstream)
    async with db_session_factory() as db:
        kept = await people_store.get_person(db, tenant_id=tenant.id, person_id=bea.id)
    assert _error(first) is None, first.headers
    assert _error(second) == "This account is linked to someone else. Ask an admin.", second.headers
    assert kept is not None and kept.account_id == bea.account_id, "the second person kept theirs"


async def test_a_visitor_cannot_link_slack_or_microsoft(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ws = await funded_workspace(db_session_factory)
    visitor = await visitor_of(db_session_factory, ws)

    async def starts(client: httpx.AsyncClient) -> list[int]:
        return [
            (await client.post(f"/auth/{p}/connect", json={}, headers=_OWN)).status_code
            for p in ("slack", "microsoft")
        ]

    codes = await _link_rig(
        web_client, db_session_factory, visitor, "U7", starts, upstream=fake_slack(team=None)
    )
    assert codes == [403, 403], "a guest can't be moved onto a chat account"
