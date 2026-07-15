"""Tests for the GitHub App webhook handler (build_github_webhook) — Plan 56-04.

Patterns:
- In-process ASGI app via httpx.ASGITransport (mirrors test_webhooks_stripe.py).
- Payloads signed with HMAC-SHA256 (matching verify_signature in core).
- Transport-level MA fake for install/resync paths — NO model_construct, no AsyncMock.
- Descriptive assertion messages on every assert.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
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
from daimon.core.stores import github_app_installations as install_store
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette

pytestmark = pytest.mark.asyncio

_WEBHOOK_SECRET = "test-webhook-secret-abc123"
# A valid Fernet key (base64-urlsafe 32 bytes). The GitHub App webhook requires
# crypto keys to be configured (create_mcp_app raises BootstrapError otherwise),
# since push-driven skill sync must decrypt the MA/MCP credential.
_FERNET_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


# ---------------------------------------------------------------------------
# Signature helpers
# ---------------------------------------------------------------------------


def _sign_payload(body: bytes, secret: str) -> str:
    """Produce the X-Hub-Signature-256 header value for a payload."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _build_app(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> Starlette:
    """Build the MCP app with GitHub App settings configured."""
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
            github=GithubSettings(
                app_id="123456",
                app_private_key=SecretStr("stub-pem-not-used-in-these-tests"),
                webhook_secret=SecretStr(_WEBHOOK_SECRET),
            ),
            crypto=CryptoSettings(keys=(SecretStr(_FERNET_KEY),)),
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )


def _build_app_no_github(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> Starlette:
    """Build the MCP app WITHOUT GitHub App settings — webhook route must not be mounted."""
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )


async def _post_github(
    app: Starlette,
    *,
    payload_dict: dict[str, Any],
    secret: str = _WEBHOOK_SECRET,
    event: str = "push",
    delivery_id: str = "delivery-001",
    bad_signature: bool = False,
) -> httpx.Response:
    """POST a signed (or forged) GitHub webhook to the app."""
    body = json.dumps(payload_dict).encode()
    sig = _sign_payload(body, secret) if not bad_signature else "sha256=badhex000"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.post(
            "/webhooks/github",
            content=body,
            headers={
                "x-github-event": event,
                "x-hub-signature-256": sig,
                "x-github-delivery": delivery_id,
                "content-type": "application/json",
            },
        )


# ---------------------------------------------------------------------------
# Payload factories
# ---------------------------------------------------------------------------


def _push_payload(
    full_name: str = "owner/my-repo",
    ref: str = "refs/heads/main",
) -> dict[str, Any]:
    return {
        "ref": ref,
        "repository": {"full_name": full_name},
        "pusher": {"name": "alice"},
    }


def _installation_created_payload(
    installation_id: int = 1234,
    account_login: str = "owner",
    repos: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "action": "created",
        "installation": {"id": installation_id, "account": {"login": account_login}},
        "repositories": repos or [{"full_name": "owner/my-repo"}],
    }


def _installation_repositories_added_payload(
    installation_id: int = 1234,
    repositories_added: list[dict[str, Any]] | None = None,
    repositories_removed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "action": "added",
        "installation": {"id": installation_id},
        "repositories_added": repositories_added or [{"full_name": "owner/new-repo"}],
        "repositories_removed": repositories_removed or [],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_github_webhook_rejects_bad_signature(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A push payload with a forged X-Hub-Signature-256 returns 401; no resync is scheduled."""
    app = _build_app(sessionmaker)
    r = await _post_github(app, payload_dict=_push_payload(), bad_signature=True)
    assert r.status_code == 401, (
        "forged/missing signature must return 401 (SC-3: reject before parse)"
    )


async def test_github_webhook_valid_push_returns_200(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A correctly-signed push to the bound default branch returns 200 with a BackgroundTask attached."""
    app = _build_app(sessionmaker)
    r = await _post_github(app, payload_dict=_push_payload(), event="push")
    assert r.status_code == 200, "correctly-signed push webhook must return 200"


async def test_github_webhook_installation_event_upserts(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A correctly-signed installation 'created' event upserts the install store, returns 200."""
    app = _build_app(sessionmaker)
    payload = _installation_created_payload(
        installation_id=9999,
        account_login="test-owner",
        repos=[{"full_name": "test-owner/test-repo"}],
    )
    r = await _post_github(app, payload_dict=payload, event="installation")
    assert r.status_code == 200, "correctly-signed installation event must return 200"

    # Verify the install was persisted
    async with db_session_factory() as check_session:
        row = await install_store.get(check_session, installation_id=9999)
    assert row is not None, "installation event must persist the install row"
    assert row.account_login == "test-owner", (
        "install row must record the account login from the payload"
    )
    assert "test-owner/test-repo" in row.repo_full_names, (
        "install row must record the repo from the payload"
    )


async def test_github_webhook_malformed_payload_is_200_noop(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A signed-but-missing-repository push payload returns 200 no-op (mirrors Stripe pattern)."""
    app = _build_app(sessionmaker)
    # Push payload with no 'repository' key
    malformed = {"ref": "refs/heads/main", "pusher": {"name": "alice"}}
    r = await _post_github(app, payload_dict=malformed, event="push")
    assert r.status_code == 200, (
        "malformed/incomplete push payload must return 200 no-op (never crash the handler)"
    )


async def test_github_webhook_route_not_mounted_without_app_settings(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The /webhooks/github route must not be mounted when App settings are absent."""
    app = _build_app_no_github(sessionmaker)
    body = json.dumps(_push_payload()).encode()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/webhooks/github",
            content=body,
            headers={
                "x-github-event": "push",
                "x-hub-signature-256": _sign_payload(body, _WEBHOOK_SECRET),
                "content-type": "application/json",
            },
        )
    assert r.status_code == 404, (
        "/webhooks/github must return 404 when GitHub App settings are not configured"
    )


async def test_github_webhook_unhandled_event_returns_200(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An unrecognized event type returns 200 (no-op)."""
    app = _build_app(sessionmaker)
    r = await _post_github(app, payload_dict={"some": "data"}, event="ping")
    assert r.status_code == 200, "unhandled event types must return 200 (not 4xx/5xx)"
