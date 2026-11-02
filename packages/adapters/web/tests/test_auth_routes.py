"""The Discord sign-in flow end to end against an in-process Discord."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest
from daimon.core.stores import web_sessions as sessions_store
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from .conftest import (
    PUBLIC_URL,
    WebClientFactory,
    discord_settings,
    fake_discord,
    sign_in,
    web_settings,
)

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
_SESSION = "__Host-daimon_web"
_FLOW = "__Host-daimon_web_flow"
_USER = {"id": "u1", "username": "maya"}


def _error(response: httpx.Response) -> str:
    assert response.status_code == 303, response.text
    url = urlsplit(response.headers["location"])
    assert url.path == "/signin", url
    return parse_qs(url.query)["error"][0]


def _set_cookie(response: httpx.Response, name: str) -> str:
    return next(h for h in response.headers.get_list("set-cookie") if h.startswith(f"{name}="))


async def test_providers_lists_discord_once_configured(web_client: WebClientFactory) -> None:
    async with web_client(settings=discord_settings()) as client:
        configured = (await client.get("/auth/providers")).json()
    async with web_client() as client:
        bare = (await client.get("/auth/providers")).json()
    assert configured == [{"id": "discord", "label": "Discord", "kind": "redirect"}], configured
    assert bare == [], "without the Discord app there is no way to sign in"


async def test_providers_offers_the_dev_user_only_in_dev(web_client: WebClientFactory) -> None:
    async with web_client(web_settings(dev=True, public_url="http://localhost:5173")) as client:
        dev = (await client.get("/auth/providers")).json()
    async with web_client() as client:
        prod = (await client.get("/auth/providers")).json()
    assert {"id": "dev", "label": "Dev user", "kind": "dev"} in dev, dev
    assert all(p["id"] != "dev" for p in prod), "production never offers the dev user"


async def test_start_sends_the_browser_to_discord_with_pkce_and_state(
    web_client: WebClientFactory,
) -> None:
    async with web_client(settings=discord_settings()) as client:
        res = await client.get("/auth/discord/start")
    url = urlsplit(res.headers["location"])
    query = parse_qs(url.query)
    assert url.netloc == "discord.com", url
    assert query["response_type"] == ["code"] and query["client_id"] == ["dc"], query
    assert query["redirect_uri"] == [f"{PUBLIC_URL}/auth/discord/callback"], query
    assert query["code_challenge_method"] == ["S256"], query
    assert query["prompt"] == ["none"], "a returning person is signed in without a screen"
    assert len(query["state"][0]) >= 24, "state is unguessable"
    assert len(query["code_challenge"][0]) == 43, "an unpadded sha256"


async def test_the_flow_cookie_is_host_bound_http_only_and_short_lived(
    web_client: WebClientFactory,
) -> None:
    async with web_client(settings=discord_settings()) as client:
        res = await client.get("/auth/discord/start")
    cookie = _set_cookie(res, _FLOW).lower()
    for flag in ("httponly", "secure", "samesite=lax", "path=/", "max-age=600"):
        assert flag in cookie, flag
    assert "domain" not in cookie, "a Host- cookie may not carry a Domain"
    value = cookie.split(";")[0].split("=", 1)[1]
    query = parse_qs(urlsplit(res.headers["location"]).query)
    assert query["state"][0].lower() not in value, "the flow is sealed, not readable"


async def test_start_for_an_unknown_method_is_404(web_client: WebClientFactory) -> None:
    async with web_client(settings=discord_settings()) as client:
        assert (await client.get("/auth/nope/start")).status_code == 404, "unknown name"
        assert (await client.get("/auth/oidc/start")).status_code == 404, "single sign-on is gone"
    async with web_client() as client:
        res = await client.get("/auth/discord/start")
    assert res.status_code == 404, "Discord is not offered until its app is configured"


async def test_sign_in_sets_the_session_cookie_and_lands_on_next(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    async with web_client(
        sessionmaker=db_session_factory,
        provider=fake_discord(_USER, [("g1", "Acme")]),
        settings=discord_settings(),
    ) as client:
        res = await sign_in(client, next="/agents/new")
        assert res.status_code == 303, res.text
        assert res.headers["location"] == "/agents/new", "the person lands where they were going"
        lowered = _set_cookie(res, _SESSION).lower()
        for flag in ("httponly", "secure", "samesite=lax", "path=/", f"max-age={30 * 86400}"):
            assert flag in lowered, flag
        assert "domain" not in lowered, "a Host- cookie may not carry a Domain"
        assert f"{_FLOW.lower()}=" in " ".join(res.headers.get_list("set-cookie")).lower(), (
            "the flow cookie is cleared"
        )
        me = await client.get("/api/me")
        assert me.status_code == 200 and me.json()["name"] == "maya", me.text
        token = client.cookies[_SESSION]
    async with db_session_factory() as session:
        stored = await session.scalar(text("select count(*) from web_sessions"))
        by_hash = await sessions_store.get_live_session(
            session,
            token_sha256=hashlib.sha256(token.encode()).digest(),
            now=datetime.now(UTC),
        )
        raw = (await session.execute(text("select token_sha256 from web_sessions"))).scalar_one()
    assert stored == 1 and by_hash is not None, "one live session"
    assert bytes(raw) == hashlib.sha256(token.encode()).digest(), "only the hash is stored"
    assert token.encode() not in bytes(raw), "the token itself is never stored"


@pytest.mark.parametrize(
    "hostile", ["https://evil.example.net/", "//evil.example.net", "/\\evil.example.net", "x"]
)
async def test_a_next_that_leaves_the_site_is_ignored(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    hostile: str,
) -> None:
    await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    async with web_client(
        sessionmaker=db_session_factory,
        provider=fake_discord(_USER, [("g1", "Acme")]),
        settings=discord_settings(),
    ) as client:
        res = await sign_in(client, next=hostile)
    assert res.headers["location"] == "/chat", "an off-site next falls back to the default"


async def test_a_state_that_does_not_match_is_refused(web_client: WebClientFactory) -> None:
    async with web_client(
        provider=fake_discord(_USER, [("g1", "Acme")]), settings=discord_settings()
    ) as client:
        await client.get("/auth/discord/start")
        res = await client.get("/auth/discord/callback", params={"code": "c", "state": "forged"})
        assert _SESSION not in client.cookies, "no session was opened"
    assert _error(res) == "Your sign-in took too long. Try again."


async def test_a_callback_without_the_flow_cookie_is_refused(
    web_client: WebClientFactory,
) -> None:
    async with web_client(
        provider=fake_discord(_USER, [("g1", "Acme")]), settings=discord_settings()
    ) as client:
        start = await client.get("/auth/discord/start")
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        client.cookies.clear()
        res = await client.get("/auth/discord/callback", params={"code": "c", "state": state})
    assert _error(res) == "Your sign-in took too long. Try again."


async def test_a_flow_cookie_from_another_deployment_key_is_refused(
    web_client: WebClientFactory,
) -> None:
    settings = discord_settings()
    async with web_client(settings=settings) as one, web_client(settings=settings) as two:
        start = await one.get("/auth/discord/start")
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        two.cookies.set(_FLOW, one.cookies[_FLOW])
        res = await two.get("/auth/discord/callback", params={"code": "c", "state": state})
    assert _error(res) == "Your sign-in took too long. Try again."


async def test_a_provider_error_returns_to_sign_in(web_client: WebClientFactory) -> None:
    async with web_client(settings=discord_settings()) as client:
        await client.get("/auth/discord/start")
        with capture_logs() as logs:
            denied = await client.get("/auth/discord/callback", params={"error": "access_denied"})
            other = await client.get("/auth/discord/callback", params={"error": "server_error"})
    assert _error(denied) == "Sign-in was cancelled."
    assert _error(other) == "Sign-in didn't work. Try again."
    assert [(e["event"], e["provider"], e["error"]) for e in logs] == [
        ("web.signin.provider_error", "discord", "access_denied"),
        ("web.signin.provider_error", "discord", "server_error"),
    ], "an operator can see what the provider answered"


async def test_a_refused_sign_in_is_logged_with_its_workspaces(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with web_client(
        sessionmaker=db_session_factory,
        provider=fake_discord(_USER, [("g-none", "Elsewhere")]),
        settings=discord_settings(),
    ) as client:
        with capture_logs() as logs:
            res = await sign_in(client)
    assert _error(res) == "Daimon isn't in any of your workspaces yet. Ask an admin."
    refused = [e for e in logs if e["event"] == "web.signin.refused"]
    assert [(e["provider"], e["workspaces"]) for e in refused] == [("discord", ["g-none"])], (
        "an operator can see which provider and workspaces turned the person away"
    )


async def test_logout_ends_the_session_and_clears_the_cookie(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    async with web_client(
        sessionmaker=db_session_factory,
        provider=fake_discord(_USER, [("g1", "Acme")]),
        settings=discord_settings(),
    ) as client:
        await sign_in(client)
        token = client.cookies[_SESSION]
        out = await client.post("/auth/logout", headers=_OWN)
        assert out.status_code == 200, out.text
        assert "max-age=0" in _set_cookie(out, _SESSION).lower(), "the cookie is cleared"
        client.cookies.set(_SESSION, token)  # a stolen copy of the old cookie
        assert (await client.get("/api/me")).status_code == 401, "the session is gone server-side"
        again = await client.post("/auth/logout", headers=_OWN)
    assert again.status_code == 200, "logging out twice is fine"


async def test_logout_needs_the_csrf_header(web_client: WebClientFactory) -> None:
    async with web_client() as client:
        res = await client.post("/auth/logout")
    assert res.status_code == 403, "a cross-site logout is refused"


async def test_several_workspaces_go_to_the_chooser(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    one = await make_tenant(db_session, platform="discord", workspace_id="g1")
    two = await make_tenant(db_session, platform="discord", workspace_id="g2")
    await db_session.commit()
    async with web_client(
        sessionmaker=db_session_factory,
        provider=fake_discord(_USER, [("g1", "One"), ("g2", "Two")]),
        settings=discord_settings(),
    ) as client:
        res = await sign_in(client, next="/agents")
        assert res.headers["location"] == f"/workspaces?next={quote('/agents', safe='')}", (
            "more than one workspace asks which to enter"
        )
        me = (await client.get("/api/me")).json()
        assert {w["id"] for w in me["workspaces"]} == {str(one.id), str(two.id)}, me
        first = me["workspace"]["id"]
        other = str(two.id) if first == str(one.id) else str(one.id)
        switched = await client.post("/api/workspaces/switch", json={"id": other}, headers=_OWN)
        assert switched.status_code == 200, switched.text
        assert (await client.get("/api/me")).json()["workspace"]["id"] == other, "switch applies"


async def test_dev_sign_in_is_absent_unless_dev(web_client: WebClientFactory) -> None:
    async with web_client() as client:
        res = await client.post("/auth/mock", json={"provider": "dev"}, headers=_OWN)
    assert res.status_code == 404, "the dev route does not exist in production"


async def test_dev_sign_in_works_over_plain_http_without_the_host_prefix(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    web = web_settings(dev=True, public_url="http://localhost:5173")
    async with web_client(web, sessionmaker=db_session_factory) as client:
        res = await client.post(
            "/auth/mock",
            json={"provider": "dev", "email": "grace@example.com"},
            headers={"X-Daimon-Web": "1", "Origin": "http://localhost:5173"},
        )
        assert res.status_code == 200, res.text
        cookie = res.headers["set-cookie"]
        assert cookie.startswith("daimon_web=") and "secure" not in cookie.lower(), cookie
        assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower(), cookie
        me = await client.get("/api/me")
    assert me.json()["name"] == "Grace" and me.json()["dev"] is True, me.text


async def test_dev_sign_in_refuses_other_methods(web_client: WebClientFactory) -> None:
    web = web_settings(dev=True, public_url="http://localhost:5173")
    async with web_client(web) as client:
        res = await client.post(
            "/auth/mock",
            json={"provider": "discord"},
            headers={"X-Daimon-Web": "1", "Origin": "http://localhost:5173"},
        )
    assert res.status_code == 400, "only the dev user signs in through the mock route"
