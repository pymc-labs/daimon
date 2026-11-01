"""The sign-in flow end to end against an in-process provider."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest
from daimon.adapters.web import auth as auth_module
from daimon.core.config import WebOidcSettings
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.web_oidc import code_challenge_s256
from daimon.testing.factories import make_tenant, make_web_person
from daimon.testing.oidc import FakeOidcProvider
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from .conftest import ISSUER, PUBLIC_URL, WebClientFactory, sign_in, web_settings

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}


def _error(response: httpx.Response) -> str:
    assert response.status_code == 303
    url = urlsplit(response.headers["location"])
    assert url.path == "/signin"
    return parse_qs(url.query)["error"][0]


def _set_cookie(response: httpx.Response, name: str) -> str:
    return next(h for h in response.headers.get_list("set-cookie") if h.startswith(f"{name}="))


async def test_providers_lists_the_configured_methods(web_client: WebClientFactory) -> None:
    oidc = WebOidcSettings(
        issuer=ISSUER,
        client_id="c",
        client_secret="s",  # pyright: ignore[reportArgumentType]
        tenant_id=web_settings().oidc.tenant_id,  # pyright: ignore[reportOptionalMemberAccess]
        label="Example SSO",
        icon="key",
    )
    async with web_client(web_settings(oidc=oidc)) as client:
        res = await client.get("/auth/providers")
    assert res.json() == [{"id": "oidc", "label": "Example SSO", "kind": "redirect", "icon": "key"}]


async def test_providers_offers_the_dev_user_only_in_dev(web_client: WebClientFactory) -> None:
    async with web_client(web_settings(dev=True, public_url="http://localhost:5173")) as client:
        dev = (await client.get("/auth/providers")).json()
    async with web_client() as client:
        prod = (await client.get("/auth/providers")).json()
    assert {"id": "dev", "label": "Dev user", "kind": "dev"} in dev
    assert all(p["id"] != "dev" for p in prod)


async def test_start_sends_the_browser_to_the_provider_with_pkce_state_and_nonce(
    web_client: WebClientFactory,
) -> None:
    provider = FakeOidcProvider()
    async with web_client(provider=provider) as client:
        res = await client.get("/auth/oidc/start")
    url = urlsplit(res.headers["location"])
    query = parse_qs(url.query)
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ISSUER}/authorize"
    assert query["response_type"] == ["code"] and query["client_id"] == ["web-client"]
    assert query["redirect_uri"] == [f"{PUBLIC_URL}/auth/oidc/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["openid email profile"]
    assert len(query["state"][0]) >= 24 and len(query["nonce"][0]) >= 24
    assert len(query["code_challenge"][0]) == 43, "an unpadded sha256"


async def test_the_flow_cookie_is_host_bound_http_only_and_short_lived(
    web_client: WebClientFactory,
) -> None:
    async with web_client(provider=FakeOidcProvider()) as client:
        res = await client.get("/auth/oidc/start")
    cookie = _set_cookie(res, "__Host-daimon_web_flow").lower()
    for flag in ("httponly", "secure", "samesite=lax", "path=/", "max-age=600"):
        assert flag in cookie, flag
    assert "domain" not in cookie, "a Host- cookie may not carry a Domain"
    value = cookie.split(";")[0].split("=", 1)[1]
    query = parse_qs(urlsplit(res.headers["location"]).query)
    assert query["state"][0].lower() not in value, "the flow is sealed, not readable"


async def test_start_for_an_unknown_method_is_404(web_client: WebClientFactory) -> None:
    async with web_client(provider=FakeOidcProvider()) as client:
        assert (await client.get("/auth/nope/start")).status_code == 404
        assert (await client.get("/auth/discord/start")).status_code == 404


async def test_sign_in_sets_the_session_cookie_and_lands_on_next(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    provider = FakeOidcProvider()
    async with web_client(
        web_settings(tenant.id), sessionmaker=db_session_factory, provider=provider
    ) as client:
        res = await sign_in(client, provider, next="/agents/new")
        assert res.status_code == 303 and res.headers["location"] == "/agents/new"
        cookie = _set_cookie(res, "__Host-daimon_web")
        lowered = cookie.lower()
        for flag in ("httponly", "secure", "samesite=lax", "path=/", f"max-age={30 * 86400}"):
            assert flag in lowered, flag
        assert "domain" not in lowered
        assert "__host-daimon_web_flow=" in " ".join(res.headers.get_list("set-cookie")).lower()
        me = await client.get("/api/me")
        assert me.status_code == 200 and me.json()["email"] == "ada@example.com"
        token = client.cookies["__Host-daimon_web"]
    async with db_session_factory() as session:
        stored = await session.scalar(text("select count(*) from web_sessions"))
        by_hash = await sessions_store.get_live_session(
            session,
            token_sha256=hashlib.sha256(token.encode()).digest(),
            now=datetime.now(UTC),
        )
        raw = (await session.execute(text("select token_sha256 from web_sessions"))).scalar_one()
    assert stored == 1 and by_hash is not None
    assert bytes(raw) == hashlib.sha256(token.encode()).digest(), "only the hash is stored"
    assert token.encode() not in bytes(raw)


@pytest.mark.parametrize(
    "hostile", ["https://evil.example.net/", "//evil.example.net", "/\\evil.example.net", "x"]
)
async def test_a_next_that_leaves_the_site_is_ignored(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    hostile: str,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    provider = FakeOidcProvider()
    async with web_client(
        web_settings(tenant.id), sessionmaker=db_session_factory, provider=provider
    ) as client:
        res = await sign_in(client, provider, next=hostile)
    assert res.headers["location"] == "/chat"


async def test_sign_in_tokens_are_kept_encrypted_with_or_without_a_refresh_token(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    provider = FakeOidcProvider()
    async with web_client(
        web_settings(tenant.id), sessionmaker=db_session_factory, provider=provider
    ) as client:
        await sign_in(client, provider, subject="plain")
        await sign_in(client, provider, subject="with-refresh", refresh_token="refresh-secret-1")
    async with db_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "select refresh_token_enc from web_oidc_tokens t join web_identities i"
                    " on i.person_id = t.person_id order by i.subject"
                )
            )
        ).all()
    assert len(rows) == 2, "both sign-ins keep their access token"
    assert rows[0][0] is None, "no refresh token came for the first"
    assert b"refresh-secret-1" not in bytes(rows[1][0]), "ciphertext only"


async def test_a_state_that_does_not_match_is_refused(web_client: WebClientFactory) -> None:
    provider = FakeOidcProvider()
    async with web_client(provider=provider) as client:
        start = await client.get("/auth/oidc/start")
        query = parse_qs(urlsplit(start.headers["location"]).query)
        code = provider.issue_code(
            code_challenge=query["code_challenge"][0],
            id_token=provider.id_token(nonce=query["nonce"][0]),
        )
        res = await client.get("/auth/oidc/callback", params={"code": code, "state": "forged"})
        assert "__Host-daimon_web" not in client.cookies
    assert _error(res) == "Your sign-in took too long. Try again."
    assert provider.token_requests == [], "the code was never redeemed"


async def test_a_callback_without_the_flow_cookie_is_refused(
    web_client: WebClientFactory,
) -> None:
    provider = FakeOidcProvider()
    async with web_client(provider=provider) as client:
        start = await client.get("/auth/oidc/start")
        query = parse_qs(urlsplit(start.headers["location"]).query)
        client.cookies.clear()
        res = await client.get(
            "/auth/oidc/callback", params={"code": "c", "state": query["state"][0]}
        )
    assert _error(res) == "Your sign-in took too long. Try again."


async def test_a_flow_cookie_from_another_deployment_key_is_refused(
    web_client: WebClientFactory,
) -> None:
    provider = FakeOidcProvider()
    async with web_client(provider=provider) as one, web_client(provider=provider) as two:
        start = await one.get("/auth/oidc/start")
        query = parse_qs(urlsplit(start.headers["location"]).query)
        two.cookies.set("__Host-daimon_web_flow", one.cookies["__Host-daimon_web_flow"])
        res = await two.get("/auth/oidc/callback", params={"code": "c", "state": query["state"][0]})
    assert _error(res) == "Your sign-in took too long. Try again."


async def test_an_id_token_for_another_nonce_is_refused(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    provider = FakeOidcProvider()
    async with web_client(
        web_settings(tenant.id), sessionmaker=db_session_factory, provider=provider
    ) as client:
        start = await client.get("/auth/oidc/start")
        query = parse_qs(urlsplit(start.headers["location"]).query)
        code = provider.issue_code(
            code_challenge=query["code_challenge"][0],
            id_token=provider.id_token(nonce="someone-elses-nonce"),
        )
        res = await client.get(
            "/auth/oidc/callback", params={"code": code, "state": query["state"][0]}
        )
        assert (await client.get("/api/me")).status_code == 401
    assert _error(res) == "Sign-in didn't work. Try again."


async def test_a_code_bound_to_another_pkce_challenge_is_refused(
    web_client: WebClientFactory,
) -> None:
    provider = FakeOidcProvider()
    async with web_client(provider=provider) as client:
        start = await client.get("/auth/oidc/start")
        query = parse_qs(urlsplit(start.headers["location"]).query)
        code = provider.issue_code(
            code_challenge=code_challenge_s256("another-verifier"),
            id_token=provider.id_token(nonce=query["nonce"][0]),
        )
        res = await client.get(
            "/auth/oidc/callback", params={"code": code, "state": query["state"][0]}
        )
    assert _error(res) == "Sign-in didn't work. Try again."


async def test_a_provider_error_returns_to_sign_in(web_client: WebClientFactory) -> None:
    async with web_client(provider=FakeOidcProvider()) as client:
        await client.get("/auth/oidc/start")
        with capture_logs() as logs:
            denied = await client.get("/auth/oidc/callback", params={"error": "access_denied"})
            other = await client.get("/auth/oidc/callback", params={"error": "server_error"})
    assert _error(denied) == "Sign-in was cancelled."
    assert _error(other) == "Sign-in didn't work. Try again."
    assert [(e["event"], e["provider"], e["error"]) for e in logs] == [
        ("web.signin.provider_error", "oidc", "access_denied"),
        ("web.signin.provider_error", "oidc", "server_error"),
    ], "an operator can see what the provider answered"


async def test_a_person_the_workspace_refuses_is_sent_back_with_the_reason(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    provider = FakeOidcProvider()
    web = web_settings(tenant.id)
    assert web.oidc is not None
    web = web.model_copy(
        update={"oidc": web.oidc.model_copy(update={"allowed_domains": ("example.org",)})}
    )
    async with web_client(web, sessionmaker=db_session_factory, provider=provider) as client:
        with capture_logs() as logs:
            res = await sign_in(client, provider)
    assert _error(res) == "This account can't sign in here. Ask an admin."
    refused = [e for e in logs if e["event"] == "web.signin.refused"]
    assert [(e["provider"], e["issuer"], e["tenant"]) for e in refused] == [
        ("oidc", ISSUER, str(tenant.id))
    ], "an operator can see which provider, issuer and workspace turned the person away"


async def test_logout_ends_the_session_and_clears_the_cookie(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    provider = FakeOidcProvider()
    async with web_client(
        web_settings(tenant.id), sessionmaker=db_session_factory, provider=provider
    ) as client:
        await sign_in(client, provider)
        token = client.cookies["__Host-daimon_web"]
        out = await client.post("/auth/logout", headers=_OWN)
        assert out.status_code == 200
        assert "max-age=0" in _set_cookie(out, "__Host-daimon_web").lower()
        client.cookies.set("__Host-daimon_web", token)  # a stolen copy of the old cookie
        assert (await client.get("/api/me")).status_code == 401
        again = await client.post("/auth/logout", headers=_OWN)
    assert again.status_code == 200, "logging out twice is fine"


async def test_logout_needs_the_csrf_header(web_client: WebClientFactory) -> None:
    async with web_client() as client:
        assert (await client.post("/auth/logout")).status_code == 403


async def test_several_workspaces_go_to_the_chooser(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one = await make_web_person(db_session)
    two = await make_web_person(db_session)
    await db_session.commit()
    provider = FakeOidcProvider()

    async def two_people(*_: object, **__: object) -> list[object]:
        return [one, two]

    monkeypatch.setattr(auth_module, "resolve_web_people", two_people)
    async with web_client(sessionmaker=db_session_factory, provider=provider) as client:
        res = await sign_in(client, provider, next="/agents")
        assert res.headers["location"] == f"/workspaces?next={quote('/agents', safe='')}"
        me = (await client.get("/api/me")).json()
        assert {w["id"] for w in me["workspaces"]} == {str(one.tenant_id), str(two.tenant_id)}
        assert me["workspace"]["id"] == str(one.tenant_id)
        switched = await client.post(
            "/api/workspaces/switch", json={"id": str(two.tenant_id)}, headers=_OWN
        )
        assert switched.status_code == 200
        assert (await client.get("/api/me")).json()["workspace"]["id"] == str(two.tenant_id)


async def test_dev_sign_in_is_absent_unless_dev(
    web_client: WebClientFactory,
) -> None:
    async with web_client() as client:
        res = await client.post("/auth/mock", json={"provider": "dev"}, headers=_OWN)
    assert res.status_code == 404


async def test_dev_sign_in_works_over_plain_http_without_the_host_prefix(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    web = web_settings(tenant.id, dev=True, public_url="http://localhost:5173")
    async with web_client(web, sessionmaker=db_session_factory) as client:
        res = await client.post(
            "/auth/mock",
            json={"provider": "dev", "email": "grace@example.com"},
            headers={"X-Daimon-Web": "1", "Origin": "http://localhost:5173"},
        )
        assert res.status_code == 200
        cookie = res.headers["set-cookie"]
        assert cookie.startswith("daimon_web=") and "secure" not in cookie.lower()
        assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
        me = await client.get("/api/me")
    assert me.json()["email"] == "grace@example.com" and me.json()["dev"] is True


async def test_dev_sign_in_refuses_other_methods(
    web_client: WebClientFactory,
) -> None:
    web = web_settings(dev=True, public_url="http://localhost:5173")
    async with web_client(web) as client:
        res = await client.post(
            "/auth/mock",
            json={"provider": "oidc"},
            headers={"X-Daimon-Web": "1", "Origin": "http://localhost:5173"},
        )
    assert res.status_code == 400
