"""Tests for the Stripe Checkout Session creation route — TOPUP-02."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl

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
from stripe._http_client import HTTPClient as StripeHTTPClient

pytestmark = pytest.mark.asyncio

_VALID_TOKEN = "bearer-test-token"
_TENANT_ID: uuid.UUID = uuid.uuid4()

# Canned Checkout Session response from Stripe API.
_CANNED_SESSION: dict[str, Any] = {
    "id": "cs_test_abc123",
    "object": "checkout.session",
    "url": "https://checkout.stripe.com/c/pay/cs_test_abc123",
    "mode": "payment",
    "status": "open",
    "metadata": {
        "tenant_id": str(_TENANT_ID),
    },
}


class _FakeStripeHTTPClient(StripeHTTPClient):
    """Fake Stripe HTTP transport that returns canned JSON for all requests.

    Transport-level fake per guideline:testing T3 — never AsyncMock on create_async.
    The real Stripe SDK serializes the request body and parses the JSON response,
    so this exercises the full SDK code path including response validation.
    """

    name = "fake"

    def __init__(self, response_body: dict[str, Any]) -> None:
        super().__init__()
        self._response_body = response_body
        # Captured form-encoded request body of the most recent request, so tests
        # can assert on what the handler actually sent rather than the canned reply.
        self.last_post_data: str | None = None

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        post_data: object = None,
    ) -> tuple[bytes, int, Mapping[str, str]]:
        self.last_post_data = _decode_post_data(post_data)
        body = json.dumps(self._response_body).encode()
        return body, 200, {"Content-Type": "application/json"}

    async def request_async(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        post_data: object = None,
    ) -> tuple[bytes, int, Mapping[str, str]]:
        self.last_post_data = _decode_post_data(post_data)
        body = json.dumps(self._response_body).encode()
        return body, 200, {"Content-Type": "application/json"}


def _decode_post_data(post_data: object) -> str | None:
    """Normalize the Stripe SDK's form-encoded request body to a str."""
    if post_data is None:
        return None
    if isinstance(post_data, bytes):
        return post_data.decode()
    if isinstance(post_data, str):
        return post_data
    raise TypeError(f"unexpected post_data type: {type(post_data)!r}")


def _sent_metadata_keys(post_data: str) -> set[str]:
    """Extract the set of metadata.* keys from a Stripe form-encoded request body.

    Stripe encodes nested params as `metadata[tenant_id]=...`; after URL-decoding
    the key reads literally `metadata[tenant_id]`. This returns `{"tenant_id"}`.
    """
    keys: set[str] = set()
    for key, _value in parse_qsl(post_data):
        match = re.fullmatch(r"metadata\[(.+)\]", key)
        if match:
            keys.add(match.group(1))
    return keys


def _build_billing_config() -> BillingConfig:
    return BillingConfig(
        secret_key=SecretStr("sk_test_checkout"),
        webhook_secret=SecretStr("whsec_test"),
        prices={10: "price_10", 25: "price_25", 50: "price_50", 100: "price_100"},
        success_url="http://test/success",
        cancel_url="http://test/cancel",
    )


def _build_app(
    sessionmaker: async_sessionmaker[AsyncSession],
    stripe_http_client: _FakeStripeHTTPClient | None = None,
) -> Starlette:
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(
            tokens={_VALID_TOKEN: {"client_id": "test", "tenant_id": str(_TENANT_ID)}}
        ),
        billing_config=_build_billing_config(),
        stripe_http_client=stripe_http_client or _FakeStripeHTTPClient(_CANNED_SESSION),
    )


async def test_checkout_route_creates_session_and_returns_url(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A valid POST with known amount returns {"url": ...} from Stripe."""
    app = _build_app(sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/billing/checkout",
            json={"amount": 10},
            headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
        )
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert "url" in body, "response must contain 'url'"
    assert body["url"] == _CANNED_SESSION["url"], "returned URL must match Stripe response"


async def test_checkout_unknown_amount_returns_422(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A POST with an amount not in prices dict returns 422."""
    app = _build_app(sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/billing/checkout",
            json={"amount": 999},
            headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
        )
    assert r.status_code == 422, "unknown amount must return 422, not 500"


async def test_billing_success_page_returns_200(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """GET /billing/success returns 200 with a static HTML body."""
    app = _build_app(sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/billing/success")
    assert r.status_code == 200, "success page must return 200"
    assert len(r.text) > 0, "success page must return non-empty body"


async def test_billing_cancel_page_returns_200(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """GET /billing/cancel returns 200 with a static HTML body."""
    app = _build_app(sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/billing/cancel")
    assert r.status_code == 200, "cancel page must return 200"
    assert len(r.text) > 0, "cancel page must return non-empty body"


async def test_unauthenticated_post_checkout_returns_401_or_403(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An unauthenticated POST /billing/checkout must be rejected — no Stripe call made."""
    app = _build_app(sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/billing/checkout",
            json={"amount": 10},
            # No Authorization header — unauthenticated
        )
    assert r.status_code in (401, 403), (
        f"unauthenticated checkout POST must return 401 or 403, got {r.status_code}"
    )


async def test_forged_bearer_token_checkout_returns_401_or_403(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An invalid/forged JWT must be rejected — 401 or 403."""
    app = _build_app(sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/billing/checkout",
            json={"amount": 10},
            headers={"Authorization": "Bearer forged-invalid-token"},
        )
    assert r.status_code in (401, 403), (
        f"invalid bearer token must return 401 or 403, got {r.status_code}"
    )


async def test_success_cancel_pages_are_public(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Success and cancel pages are publicly accessible — no auth header needed."""
    app = _build_app(sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        success = await ac.get("/billing/success")
        cancel = await ac.get("/billing/cancel")
    assert success.status_code == 200, "success page must be public (no auth)"
    assert cancel.status_code == 200, "cancel page must be public (no auth)"


# ---------------------------------------------------------------------------
# WR-01 / D-03: checkout must reject tokens without a tenant_id claim (fail-closed)
# ---------------------------------------------------------------------------


async def test_checkout_missing_tenant_id_claim_returns_403(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """WR-01 / D-03: a token with no tenant_id claim must be rejected with 403.

    The handler sources tenant_id exclusively from the verified JWT claim. A claim-less
    token (e.g. a static test token or an operator token without tenant scope) must
    be rejected fail-closed before any Stripe call is made.
    """
    token_without_tenant = "token-no-tenant-claim"
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(
            tokens={token_without_tenant: {"client_id": "test"}}  # no tenant_id claim
        ),
        billing_config=_build_billing_config(),
        stripe_http_client=_FakeStripeHTTPClient(_CANNED_SESSION),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/billing/checkout",
            json={"amount": 10},
            headers={"Authorization": f"Bearer {token_without_tenant}"},
        )
    assert r.status_code == 403, (
        f"token with no tenant_id claim must be rejected with 403; got {r.status_code}"
    )


async def test_checkout_metadata_keys_are_tenant_id_only(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Checkout session metadata the handler SENDS to Stripe is exactly {tenant_id}.

    Asserts on the form-encoded request body the handler emitted (captured by the
    fake transport), not the canned response — the request is what carries the
    credit-target metadata, and it must not leak platform/guild_id/amount_usd.
    """
    stripe_http_client = _FakeStripeHTTPClient(_CANNED_SESSION)
    app = _build_app(sessionmaker, stripe_http_client=stripe_http_client)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/billing/checkout",
            json={"amount": 10},
            headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
        )
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    assert stripe_http_client.last_post_data is not None, (
        "fake transport must have captured the Stripe request body"
    )
    sent_keys = _sent_metadata_keys(stripe_http_client.last_post_data)
    assert sent_keys == {"tenant_id"}, (
        "handler must send exactly {tenant_id} as checkout metadata — "
        f"no platform/guild_id/amount_usd; got {sent_keys}"
    )
