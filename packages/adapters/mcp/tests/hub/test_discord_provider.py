"""Discord login resolves the user's guilds to installed tenants at exchange time."""

from __future__ import annotations

import httpx
import pytest
from daimon.adapters.mcp.hub.discord_provider import DaimonDiscordProvider, fetch_discord_workspaces
from daimon.testing.factories import make_tenant
from key_value.aio.stores.memory import MemoryStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


def _discord_http(*, user_id: str, guilds: list[tuple[str, str]]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer upstream-tok", request.headers
        if request.url.path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": user_id, "username": "j"})
        if request.url.path == "/api/v10/users/@me/guilds":
            return httpx.Response(200, json=[{"id": g, "name": n} for g, n in guilds])
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://discord.com")


async def test_fetch_discord_workspaces_returns_user_and_guilds() -> None:
    http = _discord_http(user_id="u1", guilds=[("g1", "PyMC"), ("g2", "Bayes")])
    user, guilds = await fetch_discord_workspaces(http, access_token="upstream-tok")
    assert user == "u1", f"got {user!r}"
    assert guilds == [("g1", "PyMC"), ("g2", "Bayes")], f"got {guilds!r}"


def _provider(
    session_factory: async_sessionmaker[AsyncSession], http: httpx.AsyncClient
) -> DaimonDiscordProvider:
    return DaimonDiscordProvider(
        client_id="cid",
        client_secret="csecret",
        base_url="https://example.test/discord",
        session_factory=session_factory,
        client_storage=MemoryStore(),
        jwt_signing_key=b"0" * 32,
        http_client=http,
    )


async def test_extract_upstream_claims_bakes_tenant_map(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    installed = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    provider = _provider(
        db_session_factory, _discord_http(user_id="u1", guilds=[("g1", "PyMC"), ("g9", "Nope")])
    )

    claims = await provider._extract_upstream_claims({"access_token": "upstream-tok"})  # pyright: ignore[reportPrivateUsage]

    assert claims is not None, "claims must be produced for a successful exchange"
    assert claims["platform"] == "discord" and claims["platform_user_id"] == "u1", f"got {claims!r}"
    assert [t["tenant_id"] for t in claims["tenants"]] == [str(installed.id)], (
        f"got {claims['tenants']!r}"
    )
    assert claims["tenants"][0]["workspace_name"] == "PyMC"


async def test_extract_upstream_claims_with_no_shared_guilds_yields_empty_tenants(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = _provider(db_session_factory, _discord_http(user_id="u1", guilds=[("g9", "Nope")]))
    claims = await provider._extract_upstream_claims({"access_token": "upstream-tok"})  # pyright: ignore[reportPrivateUsage]
    assert claims is not None and claims["tenants"] == [], (
        f"login must succeed with no tenants, got {claims!r}"
    )


async def test_cookie_name_is_platform_specific(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = _provider(db_session_factory, _discord_http(user_id="u1", guilds=[]))
    assert provider._cookie_name("MCP_CONSENT_BINDING").endswith("MCP_CONSENT_BINDING_DISCORD"), (  # pyright: ignore[reportPrivateUsage]
        "consent cookie must not collide with the slack proxy on the same origin"
    )


async def test_requests_guilds_scope(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    provider = _provider(db_session_factory, _discord_http(user_id="u1", guilds=[]))
    assert "guilds" in (provider.required_scopes or []), f"got {provider.required_scopes!r}"
