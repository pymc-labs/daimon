"""Webhook route scaffold — healthz regression guard.

The Stripe webhook was turned from a 501 placeholder into a real
handler; the placeholder test was removed. The Stripe handler's behavior
lives in `test_webhooks_stripe.py`.
"""

from __future__ import annotations

import httpx
import pytest
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.billing import BillingConfig
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
)
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette

pytestmark = pytest.mark.asyncio


def _build_app(sessionmaker: async_sessionmaker[AsyncSession]) -> Starlette:
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        billing_config=BillingConfig(
            secret_key=SecretStr("sk_test"),
            webhook_secret=SecretStr("whsec_test"),
            prices={10: "p10", 25: "p25", 50: "p50", 100: "p100"},
            success_url="http://test/success",
            cancel_url="http://test/cancel",
        ),
    )


async def test_healthz_still_responds_after_webhook_routes_added(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Regression guard: webhook add_route siblings should not break /healthz."""
    app = _build_app(sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/healthz")
    assert r.status_code == 200, "healthz should still respond after webhook routes are registered"
    assert r.text == "ok", "healthz body should remain 'ok'"
