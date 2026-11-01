"""Shared fixtures for web adapter tests."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
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
from daimon.core.config import Settings, WebOidcSettings, WebSettings
from daimon.core.github_credentials import build_multifernet
from daimon.core.web_oidc import OidcCache
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
from daimon.testing.oidc import FakeOidcProvider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from stripe import StripeClient

PUBLIC_URL = "https://web.example.com"
ISSUER = "https://id.example.com"


def web_settings(tenant_id: uuid.UUID | None = None, **overrides: object) -> WebSettings:
    """Web settings for a deployment at `PUBLIC_URL` signing in through `ISSUER`."""
    fields: dict[str, object] = {
        "public_url": PUBLIC_URL,
        "oidc": WebOidcSettings(
            issuer=ISSUER,
            client_id="web-client",
            client_secret="web-secret",  # pyright: ignore[reportArgumentType]  # SecretStr coerces
            tenant_id=tenant_id or uuid.uuid4(),
        ),
    }
    return WebSettings(**{**fields, **overrides})  # pyright: ignore[reportArgumentType]


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
        provider: FakeOidcProvider | None = None,
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
                oidc=OidcCache(provider_http, web.oidc) if web.oidc else None,
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


async def sign_in(
    client: httpx.AsyncClient,
    provider: FakeOidcProvider,
    *,
    next: str | None = None,
    refresh_token: str | None = None,
    **claims: object,
) -> httpx.Response:
    """Walk the redirect flow the way a browser would and return the callback response."""
    start = await client.get("/auth/oidc/start", params={"next": next} if next else None)
    assert start.status_code == 303, start.text
    query = parse_qs(urlsplit(start.headers["location"]).query)
    token = provider.id_token(nonce=query["nonce"][0], **claims)  # pyright: ignore[reportArgumentType]
    code = provider.issue_code(
        code_challenge=query["code_challenge"][0], id_token=token, refresh_token=refresh_token
    )
    return await client.get(
        "/auth/oidc/callback", params={"code": code, "state": query["state"][0]}
    )
