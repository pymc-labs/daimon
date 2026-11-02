"""Shared fixtures for web adapter tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from daimon.adapters.web.app import create_app
from daimon.adapters.web.runtime import WebRuntime
from daimon.adapters.web.turns import WebTurns
from daimon.core.billing import BillingConfig
from daimon.core.config import Settings, WebSettings
from daimon.core.github_credentials import build_multifernet
from daimon.testing.db import (  # noqa: F401
    db_clean,
    db_committing_engine,
    db_committing_pool,
    db_engine,
    db_nullpool_engine,
    db_schema,
    db_session,
    db_session_factory,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from stripe import StripeClient

PUBLIC_URL = "https://web.example.com"


def web_settings(**overrides: object) -> WebSettings:
    """Web settings for a deployment at `PUBLIC_URL`."""
    return WebSettings(**{"public_url": PUBLIC_URL, **overrides})  # pyright: ignore[reportArgumentType]


def discord_settings(**overrides: object) -> Settings:
    """Settings with Discord sign-in configured."""
    fields: dict[str, object] = {
        "database": {"url": "postgresql+asyncpg://u:p@h/db"},
        "hub": {"discord_client_id": "dc", "discord_client_secret": "ds"},
    }
    return Settings(_env_file=None, **{**fields, **overrides})  # pyright: ignore[reportCallIssue, reportArgumentType]


@dataclass(frozen=True)
class FakeUpstream:
    """Answers the web app's outbound requests."""

    handler: Callable[[httpx.Request], httpx.Response]


def fake_discord(
    user: dict[str, str], guilds: list[tuple[str, str]], *, fallback: FakeUpstream | None = None
) -> FakeUpstream:
    """Discord's token, user and guild endpoints, answering as `user`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "discord.com":
            if request.url.path == "/api/v10/oauth2/token":
                return httpx.Response(200, json={"access_token": "dtok", "token_type": "Bearer"})
            if request.url.path == "/api/v10/users/@me":
                return httpx.Response(200, json=user)
            if request.url.path == "/api/v10/users/@me/guilds":
                return httpx.Response(200, json=[{"id": g, "name": n} for g, n in guilds])
        if fallback is not None:
            return fallback.handler(request)
        return httpx.Response(404)

    return FakeUpstream(handler)


WebClientFactory = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]


@pytest.fixture
def web_client() -> WebClientFactory:
    """`async with web_client(web, sessionmaker=...) as client:` talks to the app in-process.

    Requests go to `PUBLIC_URL`, so `Secure` cookies are sent back and the origin check sees
    the address a browser would.
    """

    @asynccontextmanager
    async def build(
        web: WebSettings | None = None,
        *,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        provider: FakeUpstream | None = None,
        now: Callable[[], datetime] | None = None,
        turns: WebTurns | None = None,
        settings: Settings | None = None,
        billing_config: BillingConfig | None = None,
        stripe: StripeClient | None = None,
        identity_key: Ed25519PrivateKey | None = None,
    ) -> AsyncIterator[httpx.AsyncClient]:
        web = web or web_settings()
        settings = settings or Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider.handler) if provider else None
        ) as provider_http:
            runtime = WebRuntime(
                settings=settings,
                web=web,
                sessionmaker=sessionmaker or async_sessionmaker(class_=AsyncSession),
                http_client=provider_http,
                fernet=build_multifernet((Fernet.generate_key().decode(),)),
                turns=turns,
                billing_config=billing_config,
                stripe=stripe,
                identity_key=identity_key,
                **({"now": now} if now else {}),
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(runtime)), base_url=PUBLIC_URL
            ) as client:
                yield client

    return build


@pytest.fixture
def built_app(tmp_path: Path) -> Path:
    """A minimal built single-page app on disk."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>app</html>")
    (tmp_path / "assets" / "main-abc123.js").write_text("console.log(1)")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    return tmp_path


async def sign_in(client: httpx.AsyncClient, *, next: str | None = None) -> httpx.Response:
    """Walk the Discord redirect flow the way a browser would; return the callback response."""
    start = await client.get("/auth/discord/start", params={"next": next} if next else None)
    assert start.status_code == 303, start.text
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    return await client.get("/auth/discord/callback", params={"code": "c", "state": state})
