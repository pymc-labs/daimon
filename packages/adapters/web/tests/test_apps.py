"""Embedded apps: the sign-in token handed to the app it is for, its status, an agent's panels."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from daimon.adapters.web.app_probe import AppStatus, AppStatusCache
from daimon.adapters.web.apps import agent_apps
from daimon.core.config import EmbeddedAppSettings
from daimon.core.stores import web_roles as roles_store
from daimon.testing.oidc import FakeOidcProvider
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .chat_support import funded_workspace
from .conftest import ISSUER, PUBLIC_URL, WebClientFactory, sign_in, web_settings

_APP = EmbeddedAppSettings(
    key="data",
    name="Data",
    icon="grid",
    origin="https://app.example.com",
    oidc_issuer=ISSUER,
)
_DOWN = EmbeddedAppSettings(key="down", name="Down", icon="grid", origin="https://down.example.com")
_OTHER = EmbeddedAppSettings(
    key="other",
    name="Other",
    icon="grid",
    origin="https://other.example.com",
    oidc_issuer="https://other-id.example.com",
)


class _Provider(FakeOidcProvider):
    """The sign-in provider, plus the apps' manifests."""

    def __init__(self) -> None:
        super().__init__(issuer=ISSUER)
        self.manifest_auth: list[str | None] = []
        self.revoked = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "app.example.com":
            self.manifest_auth.append(request.headers.get("authorization"))
            return httpx.Response(200, json={"name": "Data"})
        if request.url.host == "down.example.com":
            return httpx.Response(503)
        if self.revoked and b"grant_type=refresh_token" in request.content:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return super().handler(request)

    def refreshes(self) -> int:
        return sum(1 for r in self.token_requests if r.get("grant_type") == "refresh_token")


@dataclass
class _Clock:
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def factory(db_nullpool_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # Real commits on separate connections, so concurrent requests really contend.
    return async_sessionmaker(db_nullpool_engine, expire_on_commit=False)


@asynccontextmanager
async def _signed_in(
    web_client: WebClientFactory,
    factory: async_sessionmaker[AsyncSession],
    *,
    refresh_token: str | None = "r1",
) -> AsyncIterator[tuple[httpx.AsyncClient, _Provider, _Clock, uuid.UUID]]:
    ws = await funded_workspace(factory)
    provider, clock = _Provider(), _Clock()
    web = web_settings(ws.tenant_id, apps=(_APP, _DOWN, _OTHER))
    async with web_client(web, sessionmaker=factory, provider=provider, now=clock) as client:
        done = await sign_in(client, provider, refresh_token=refresh_token)
        assert done.status_code == 303, done.text
        client.headers.update({"X-Daimon-Web": "1", "Origin": PUBLIC_URL})
        yield client, provider, clock, ws.tenant_id


async def _token(client: httpx.AsyncClient, key: str = "data") -> httpx.Response:
    return await client.post(f"/api/apps/{key}/token")


async def test_an_app_gets_the_token_only_from_the_provider_it_trusts(
    web_client: WebClientFactory, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with _signed_in(web_client, factory) as (client, provider, *_):
        mine = await _token(client)
        other, untrusting, unknown = (
            await _token(client, "other"),
            await _token(client, "down"),
            await _token(client, "nope"),
        )

    assert mine.status_code == 200 and mine.json()["access_token"].startswith("access-"), mine.text
    assert other.status_code == 404, "another provider's app gets nothing"
    assert untrusting.status_code == 404, "an app that trusts no provider gets nothing"
    assert unknown.status_code == 404, "an unknown app gets nothing"
    assert provider.refreshes() == 0, "a fresh token is handed out as it is"


async def test_a_token_near_expiry_is_refreshed_once_when_asked_twice_at_once(
    web_client: WebClientFactory, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with _signed_in(web_client, factory) as (client, provider, clock, _):
        first = (await _token(client)).json()["access_token"]
        clock.at += timedelta(seconds=545)
        both = await asyncio.gather(_token(client), _token(client))
        clock.at += timedelta(seconds=60)
        later = (await _token(client)).json()["access_token"]

    tokens = {r.json()["access_token"] for r in both}
    assert len(tokens) == 1 and first not in tokens, "both get the one refreshed token"
    assert provider.refreshes() == 1, "the rotating refresh token is used once"
    assert later in tokens, "the refreshed token is kept and reused while fresh"


async def test_without_a_refresh_token_an_expired_token_is_not_handed_out(
    web_client: WebClientFactory, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with _signed_in(web_client, factory, refresh_token=None) as (client, _, clock, _):
        fresh = await _token(client)
        clock.at += timedelta(seconds=590)
        spent = await _token(client)

    assert fresh.status_code == 200, "the sign-in's token works while it lasts"
    assert spent.status_code == 404, "then a new sign-in is needed"


async def test_a_refused_refresh_hands_out_nothing(
    web_client: WebClientFactory, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with _signed_in(web_client, factory) as (client, provider, clock, _):
        provider.revoked = True
        clock.at += timedelta(seconds=590)
        refused = await _token(client)

    assert refused.status_code == 404, "a revoked sign-in gives the app nothing"


async def test_an_unreadable_refresh_token_hands_out_nothing(
    web_client: WebClientFactory, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with _signed_in(web_client, factory) as (client, provider, clock, tenant_id):
        async with factory.begin() as db:
            await db.execute(
                text(
                    "UPDATE web_oidc_tokens SET refresh_token_enc = 'x'::bytea WHERE tenant_id = :t"
                ),
                {"t": tenant_id},
            )
        clock.at += timedelta(seconds=590)
        unreadable = await _token(client)

    assert unreadable.status_code == 404, "an unreadable refresh token yields nothing"
    assert provider.refreshes() == 0, "and is never sent"


async def test_the_token_is_never_in_me(
    web_client: WebClientFactory, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with _signed_in(web_client, factory) as (client, *_):
        token = (await _token(client)).json()["access_token"]
        me = await client.get("/api/me")

    assert me.status_code == 200, me.text
    assert token not in me.text and "access_token" not in me.text, "Me never carries it"
    assert [a["key"] for a in me.json()["apps"]] == ["data", "down", "other"], "apps are listed"


async def test_the_apps_status_probes_each_manifest_as_the_admin_and_is_reused(
    web_client: WebClientFactory, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with _signed_in(web_client, factory) as (client, provider, _, tenant_id):
        async with factory() as db:
            person = (
                await db.execute(
                    text(
                        "SELECT p.tenant_id, p.account_id FROM web_people p JOIN web_identities i"
                        " ON i.person_id = p.id WHERE i.subject = 'user-1' AND p.tenant_id = :t"
                    ),
                    {"t": tenant_id},
                )
            ).one()
        async with factory.begin() as db:
            await roles_store.set_workspace_role(
                db,
                tenant_id=person.tenant_id,
                account_id=person.account_id,
                role="owner",
                granted_by_account_id=None,
            )
        token = (await _token(client)).json()["access_token"]
        settings = await client.get("/api/admin/settings")
        changed = await client.patch("/api/admin/settings", json={"modules": {"agents": False}})
        asked_before = len(provider.manifest_auth)
        got = await client.get("/api/admin/apps/status")
        again = await client.get("/api/admin/apps/status")

    assert settings.status_code == 200 and changed.status_code == 200, settings.text
    assert asked_before == 0, "reading or changing settings asks no app anything"
    assert all("status" not in a for a in changed.json()["apps"]), "settings carry no status"
    assert got.status_code == 200, got.text
    assert got.json() == {"data": "connected", "down": "error", "other": "error"}, got.json()
    assert again.json() == got.json(), "a second read gives the same answer"
    assert provider.manifest_auth == [f"Bearer {token}"], (
        "asked once with the admin's token; the second read came from the cache"
    )


_PANEL_APP = EmbeddedAppSettings.model_validate(
    {
        "key": "notes",
        "name": "Notes",
        "icon": "book",
        "origin": "https://notes.example.com",
        "panels": {
            "agent": {
                "embed": "/embed/agents/external/{agent_id}",
                "page": "/agents/external/{agent_id}",
            }
        },
    }
)
_AGENT = "5b1c2f56-6a39-5c1e-9a0e-3f5a2f6f2d11"


def test_an_agents_panel_paths_carry_its_id_for_an_app_it_uses() -> None:
    got = agent_apps(
        [_PANEL_APP, _APP], agent_id=_AGENT, server_urls=["https://notes.example.com/mcp"]
    )
    assert [a.model_dump() for a in got] == [
        {
            "key": "notes",
            "embed": f"/embed/agents/external/{_AGENT}",
            "page": f"/agents/external/{_AGENT}",
        }
    ], "only the app with an agent panel the agent uses, paths filled in"


def test_the_id_is_escaped_into_the_path() -> None:
    (got,) = agent_apps(
        [_PANEL_APP], agent_id="a/b?c", server_urls=["https://notes.example.com/mcp"]
    )
    assert got.embed == "/embed/agents/external/a%2Fb%3Fc", "no character leaves the segment"


def test_a_page_is_optional() -> None:
    app = _PANEL_APP.model_validate(
        {**_PANEL_APP.model_dump(), "panels": {"agent": {"embed": "/embed/{agent_id}"}}}
    )
    (got,) = agent_apps([app], agent_id=_AGENT, server_urls=["https://notes.example.com/x"])
    assert got.page is None, "an app without a full page offers no link"


@pytest.mark.parametrize(
    "urls",
    [[], ["https://other.example.com/mcp"], ["http://notes.example.com/mcp"]],
)
def test_an_agent_without_a_server_at_the_app_has_no_panel(urls: list[str]) -> None:
    assert agent_apps([_PANEL_APP], agent_id=_AGENT, server_urls=urls) == [], (
        "the origin must match, scheme included"
    )


def test_server_origins_match_regardless_of_case() -> None:
    got = agent_apps([_PANEL_APP], agent_id=_AGENT, server_urls=["https://Notes.Example.com/mcp"])
    assert len(got) == 1, "host case does not matter"


def test_server_origins_match_the_apps_with_a_default_port() -> None:
    app = EmbeddedAppSettings.model_validate(
        {**_PANEL_APP.model_dump(), "origin": "https://Notes.example.com:443"}
    )
    got = agent_apps([app], agent_id=_AGENT, server_urls=["https://notes.example.com/mcp"])
    assert len(got) == 1, "the app's origin is normalised like a server URL's"
    got = agent_apps(
        [_PANEL_APP], agent_id=_AGENT, server_urls=["https://notes.example.com:443/mcp"]
    )
    assert len(got) == 1, "a server URL's default port does not matter"


async def test_the_status_cache_is_per_person() -> None:
    cache, asked = AppStatusCache(), 0

    async def probe() -> AppStatus:
        nonlocal asked
        asked += 1
        return "connected"

    a, b = uuid.uuid4(), uuid.uuid4()
    await cache.get(a, "data", probe)
    await cache.get(a, "data", probe)
    await cache.get(b, "data", probe)
    assert asked == 2, "a second person is asked with their own token, not served the first's"
