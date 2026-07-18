"""create_mcp_app GitHub repo-auth boot (App-or-PAT).

The OAuth web flow + CLI-auth-status route are gone — no
`/oauth/github/*` route should ever be mounted.

App-clone boots with only `app_id` + `app_private_key` — no
`webhook_secret` required. The `/webhooks/github` mount (skill-sync's
push-driven resync trigger) is gated separately on `webhook_secret is not
None`, and fails loud on partial config (webhook_secret without App creds,
or without crypto keys).
"""

from __future__ import annotations

import pytest
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    GithubSettings,
    McpSettings,
    Settings,
)
from daimon.core.errors import BootstrapError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.routing import Route

pytestmark = pytest.mark.asyncio

# A valid Fernet key (base64-urlsafe 32 bytes) — see test_webhooks_github.py.
_FERNET_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def _settings(*, github: GithubSettings, crypto: CryptoSettings | None = None) -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        github=github,
        crypto=crypto or CryptoSettings(),
    )


def _route_paths(app: object) -> list[str]:
    return [r.path for r in app.routes if isinstance(r, Route)]  # type: ignore[attr-defined]


async def test_oauth_github_routes_absent(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """No /oauth/github/* or /cli/auth/status route is ever mounted."""
    app = create_mcp_app(
        settings=_settings(github=GithubSettings()),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )
    route_paths = _route_paths(app)
    assert "/oauth/github/start" not in route_paths, "OAuth start route must be gone"
    assert "/oauth/github/callback" not in route_paths, "OAuth callback route must be gone"
    assert "/cli/auth/status" not in route_paths, "cli-auth-status route must be gone"


async def test_app_clone_boots_with_only_app_id_and_private_key(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """App-clone requires only app_id + app_private_key — no webhook_secret."""
    app = create_mcp_app(
        settings=_settings(
            github=GithubSettings(
                app_id="123456",
                app_private_key=SecretStr("stub-pem-not-parsed-at-boot"),
            )
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )
    assert app is not None, "App-clone must boot without WEBHOOK_SECRET or crypto keys"
    route_paths = _route_paths(app)
    assert "/webhooks/github" not in route_paths, (
        "webhook must not mount when webhook_secret is unset, even with App creds present"
    )


def test_partial_app_config_missing_private_key_raises() -> None:
    """app_id set but app_private_key missing must fail loud, not boot silently degraded."""
    settings = _settings(github=GithubSettings(app_id="123456"))
    with pytest.raises(BootstrapError, match="app_private_key"):
        create_mcp_app(settings=settings, auth=StaticTokenVerifier(tokens={}))


def test_webhook_secret_without_app_creds_raises() -> None:
    """webhook_secret alone (no App creds) must fail loud — the webhook needs the App."""
    settings = _settings(
        github=GithubSettings(webhook_secret=SecretStr("test-webhook-secret")),
    )
    with pytest.raises(BootstrapError, match="app_id"):
        create_mcp_app(settings=settings, auth=StaticTokenVerifier(tokens={}))


def test_webhook_secret_without_fernet_raises() -> None:
    """webhook_secret + App creds but no crypto keys must fail loud (can't decrypt)."""
    settings = _settings(
        github=GithubSettings(
            app_id="123456",
            app_private_key=SecretStr("stub-pem-not-parsed-at-boot"),
            webhook_secret=SecretStr("test-webhook-secret"),
        ),
        crypto=CryptoSettings(),
    )
    with pytest.raises(BootstrapError, match="crypto"):
        create_mcp_app(settings=settings, auth=StaticTokenVerifier(tokens={}))


async def test_webhook_mounts_when_fully_configured(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Full App + webhook_secret + fernet config mounts /webhooks/github."""
    app = create_mcp_app(
        settings=_settings(
            github=GithubSettings(
                app_id="123456",
                app_private_key=SecretStr("stub-pem-not-parsed-at-boot"),
                webhook_secret=SecretStr("test-webhook-secret"),
            ),
            crypto=CryptoSettings(keys=(SecretStr(_FERNET_KEY),)),
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )
    route_paths = _route_paths(app)
    assert "/webhooks/github" in route_paths, (
        "webhook must mount when webhook_secret + App creds + fernet are all present"
    )
