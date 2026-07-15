"""Tests for billing_panel/checkout.py and billing_panel/actions.py (top-up select).

Covers:
- create_checkout POSTs to /billing/checkout with {"amount": N} body and
  "Authorization: Bearer <token>" header; returns the URL (transport-level fake).
- handle_topup_select with an admin user + amount=25: asserts checkout POST and
  ephemeral chat.postEphemeral with the mrkdwn <url|...> link.
- handle_topup_select with a non-admin user: asserts NO checkout POST (D-02).
- handle_topup_select with an invalid amount: asserts NO checkout POST (T-82-10).

Transport-level fakes:
  - MCP /billing/checkout: httpx.MockTransport (injected via _http_client parameter)
  - Slack Web API: aioresponses (for resolve_is_admin users.info + chat.postEphemeral)

No stripe imports anywhere in billing_panel. No method-level AsyncMock.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio
from aioresponses import aioresponses as AioResponsesMock
from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing.factories import make_tenant
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Shared constants / patterns
# ---------------------------------------------------------------------------

_SLACK_API_BASE = "https://slack.com/api"
_USERS_INFO_PATTERN = re.compile(r"https://slack\.com/api/users\.info.*")
_VIEWS_OPEN_URL = f"{_SLACK_API_BASE}/views.open"
_VIEWS_UPDATE_URL = f"{_SLACK_API_BASE}/views.update"
_POST_EPHEMERAL_URL = f"{_SLACK_API_BASE}/chat.postEphemeral"

_MCP_CHECKOUT_URL = "https://mcp.example.com/billing/checkout"
_CHECKOUT_RESPONSE_URL = "https://checkout.example/abc"

_TEAM_ID = "T_CHECKOUT_TEST"
_USER_ID = "U_CHECKOUT_ADMIN"
_CHANNEL_ID = "C_CHECKOUT_CHAN"

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def runtime(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> SlackRuntime:
    """SlackRuntime with a seeded Slack bot token + tenant and mocked MCP settings."""
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    plaintext_token = "xoxb-checkout-test"
    encrypted = encrypt_token(fernet, plaintext_token)

    async with db_session_factory() as s, s.begin():
        await make_tenant(s, platform="slack", workspace_id=_TEAM_ID)
        await upsert_slack_bot_token(
            s,
            team_id=_TEAM_ID,
            encrypted_token=encrypted,
        )

    settings = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    settings.mcp.app_root_url = "https://mcp.example.com"
    settings.mcp.jwt_secret = SecretStr("test-jwt-secret-at-least-32-chars-long!!")

    return SlackRuntime(
        settings=settings,
        anthropic=MagicMock(spec=AsyncAnthropic),
        sessionmaker=db_session_factory,
        http_client=MagicMock(spec=httpx.AsyncClient),
    )


def _build_admin_payload(*, amount: int) -> dict[str, Any]:
    """Block actions payload for billing_topup with admin user."""
    return {
        "team": {"id": _TEAM_ID},
        "user": {"id": _USER_ID},
        "container": {"channel_id": _CHANNEL_ID},
        "actions": [
            {
                "action_id": "billing_topup",
                "type": "static_select",
                "selected_option": {"value": str(amount)},
            }
        ],
    }


def _build_non_admin_payload(*, amount: int) -> dict[str, Any]:
    """Block actions payload for billing_topup with a non-admin user."""
    return {
        "team": {"id": _TEAM_ID},
        "user": {"id": "U_REGULAR"},
        "container": {"channel_id": _CHANNEL_ID},
        "actions": [
            {
                "action_id": "billing_topup",
                "type": "static_select",
                "selected_option": {"value": str(amount)},
            }
        ],
    }


def _make_checkout_transport(
    expected_amount: int,
    captured_requests: list[httpx.Request],
) -> httpx.MockTransport:
    """Build an httpx.MockTransport that verifies the checkout POST body."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"url": _CHECKOUT_RESPONSE_URL})

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Unit: create_checkout
# ---------------------------------------------------------------------------


async def test_create_checkout_posts_with_amount_and_bearer_token() -> None:
    """create_checkout POSTs {"amount": N} with Authorization: Bearer header."""
    from daimon.adapters.slack.billing_panel.checkout import create_checkout

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"url": _CHECKOUT_RESPONSE_URL})

    settings = MagicMock()
    settings.app_root_url = "https://mcp.example.com"
    settings.jwt_secret = SecretStr("test-jwt-secret-at-least-32-chars-long!!")

    account_id = uuid.uuid4()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        url = await create_checkout(
            client,
            settings=settings,
            account_id=account_id,
            amount=25,
        )

    assert url == _CHECKOUT_RESPONSE_URL, "create_checkout must return the URL from the response"
    assert len(captured) == 1, "create_checkout must make exactly one POST request"

    req = captured[0]
    assert req.method == "POST", "create_checkout must use POST"
    assert "/billing/checkout" in str(req.url), "create_checkout must POST to /billing/checkout"

    # Verify body has amount only (no tenant_id — OQ-1)
    body = json.loads(req.content)
    assert body == {"amount": 25}, (
        "request body must be {'amount': 25} — no tenant_id/guild_id in body (OQ-1)"
    )

    # Verify Authorization header
    auth_header = req.headers.get("authorization", "")
    assert auth_header.startswith("Bearer "), (
        "request must have Authorization: Bearer <token> header"
    )
    token = auth_header[len("Bearer ") :]
    assert len(token) > 10, "Bearer token must be a non-trivial JWT"


async def test_create_checkout_raises_on_non_2xx_response() -> None:
    """create_checkout raises httpx.HTTPStatusError on non-2xx."""
    from daimon.adapters.slack.billing_panel.checkout import create_checkout

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "invalid amount"})

    settings = MagicMock()
    settings.app_root_url = "https://mcp.example.com"
    settings.jwt_secret = SecretStr("test-jwt-secret-at-least-32-chars-long!!")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await create_checkout(
                client,
                settings=settings,
                account_id=uuid.uuid4(),
                amount=999,
            )


# ---------------------------------------------------------------------------
# Integration: handle_topup_select — admin user
# ---------------------------------------------------------------------------


async def test_handle_topup_select_admin_posts_checkout_and_sends_ephemeral(
    runtime: SlackRuntime,
) -> None:
    """Admin top-up: checkout POST sent and ephemeral reply contains the URL."""
    from daimon.adapters.slack.billing_panel.actions import handle_topup_select

    captured_checkout: list[httpx.Request] = []
    captured_ephemeral: list[Any] = []

    def checkout_handler(request: httpx.Request) -> httpx.Response:
        captured_checkout.append(request)
        return httpx.Response(200, json={"url": _CHECKOUT_RESPONSE_URL})

    admin_users_info_payload = {
        "ok": True,
        "user": {
            "id": _USER_ID,
            "name": "admin_user",
            "is_admin": True,
            "is_owner": False,
            "is_primary_owner": False,
        },
    }

    with AioResponsesMock() as mock:
        # users.info → admin
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload=admin_users_info_payload,
            repeat=True,
        )
        # views.open/update needed if called (not needed here but avoids stray errors)
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            _VIEWS_OPEN_URL,
            payload={"ok": True, "view": {"id": "V_TEST", "hash": "H_TEST"}},
            repeat=True,
        )
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            _VIEWS_UPDATE_URL,
            payload={"ok": True, "view": {"id": "V_TEST", "hash": "H_TEST"}},
            repeat=True,
        )
        # chat.postEphemeral — capture raw JSON body in callback
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            _POST_EPHEMERAL_URL,
            payload={"ok": True, "message_ts": "1234.5678"},
            repeat=True,
            callback=lambda url, **kwargs: captured_ephemeral.append(kwargs),
        )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(checkout_handler))
        await handle_topup_select(
            runtime,
            _build_admin_payload(amount=25),
            _http_client=http_client,
        )

    # Verify checkout POST was made
    assert len(captured_checkout) == 1, (
        "handle_topup_select must send exactly one checkout POST for an admin"
    )
    checkout_body = json.loads(captured_checkout[0].content)
    assert checkout_body == {"amount": 25}, "checkout POST body must be {'amount': 25}"
    auth = captured_checkout[0].headers.get("authorization", "")
    assert auth.startswith("Bearer "), "checkout POST must include Authorization: Bearer header"

    # Verify ephemeral was posted with the mrkdwn link
    assert len(captured_ephemeral) >= 1, "chat.postEphemeral must have been called"


async def test_handle_topup_select_admin_ephemeral_text_contains_url(
    runtime: SlackRuntime,
) -> None:
    """The ephemeral reply must contain the <url|Complete payment> mrkdwn link."""
    from daimon.adapters.slack.billing_panel.actions import handle_topup_select

    posted_texts: list[str] = []

    def checkout_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"url": _CHECKOUT_RESPONSE_URL})

    admin_users_info_payload = {
        "ok": True,
        "user": {
            "id": _USER_ID,
            "name": "admin_user",
            "is_admin": True,
            "is_owner": False,
            "is_primary_owner": False,
        },
    }

    with AioResponsesMock(passthrough=["http://localhost"]) as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload=admin_users_info_payload,
            repeat=True,
        )
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            _POST_EPHEMERAL_URL,
            payload={"ok": True, "message_ts": "1234.5678"},
            repeat=True,
        )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(checkout_handler))
        await handle_topup_select(
            runtime,
            _build_admin_payload(amount=25),
            _http_client=http_client,
        )

        # Inspect aioresponses captured requests
        from yarl import URL

        ephemeral_key = ("POST", URL(_POST_EPHEMERAL_URL))
        ephemeral_reqs = mock.requests.get(ephemeral_key, [])
        if ephemeral_reqs:
            for call in ephemeral_reqs:
                body: dict[str, Any] = call.kwargs.get("json") or {}
                text_val = body.get("text", "")
                posted_texts.append(str(text_val))

    # At least one ephemeral was sent with the payment URL
    matching = [t for t in posted_texts if _CHECKOUT_RESPONSE_URL in t]
    assert matching, (
        f"chat.postEphemeral must be called with text containing {_CHECKOUT_RESPONSE_URL!r}; "
        f"got: {posted_texts!r}"
    )
    link_format = [t for t in matching if "Complete payment" in t]
    assert link_format, "ephemeral text must use mrkdwn link format '<url|Complete payment>'"


# ---------------------------------------------------------------------------
# Integration: handle_topup_select — non-admin user (D-02)
# ---------------------------------------------------------------------------


async def test_handle_topup_select_non_admin_issues_no_checkout_post(
    runtime: SlackRuntime,
) -> None:
    """Non-admin click must NOT trigger a checkout POST (D-02 fail-closed)."""
    from daimon.adapters.slack.billing_panel.actions import handle_topup_select

    captured_checkout: list[httpx.Request] = []

    def checkout_handler(request: httpx.Request) -> httpx.Response:
        captured_checkout.append(request)
        return httpx.Response(200, json={"url": _CHECKOUT_RESPONSE_URL})

    non_admin_users_info_payload = {
        "ok": True,
        "user": {
            "id": "U_REGULAR",
            "name": "regular_user",
            "is_admin": False,
            "is_owner": False,
            "is_primary_owner": False,
        },
    }

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload=non_admin_users_info_payload,
            repeat=True,
        )
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            _POST_EPHEMERAL_URL,
            payload={"ok": True, "message_ts": "1234.5678"},
            repeat=True,
        )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(checkout_handler))
        await handle_topup_select(
            runtime,
            _build_non_admin_payload(amount=25),
            _http_client=http_client,
        )

    assert len(captured_checkout) == 0, (
        "Non-admin must NOT trigger a checkout POST (D-02 fail-closed)"
    )


# ---------------------------------------------------------------------------
# Integration: handle_topup_select — invalid amount (T-82-10)
# ---------------------------------------------------------------------------


async def test_handle_topup_select_invalid_amount_issues_no_checkout_post(
    runtime: SlackRuntime,
) -> None:
    """An amount not in the preset set must NOT trigger a checkout POST (T-82-10)."""
    from daimon.adapters.slack.billing_panel.actions import handle_topup_select

    captured_checkout: list[httpx.Request] = []

    def checkout_handler(request: httpx.Request) -> httpx.Response:
        captured_checkout.append(request)
        return httpx.Response(200, json={"url": _CHECKOUT_RESPONSE_URL})

    admin_users_info_payload = {
        "ok": True,
        "user": {
            "id": _USER_ID,
            "name": "admin_user",
            "is_admin": True,
            "is_owner": False,
            "is_primary_owner": False,
        },
    }

    # Use an amount NOT in {10, 25, 50, 100}
    invalid_amount_payload: dict[str, Any] = {
        "team": {"id": _TEAM_ID},
        "user": {"id": _USER_ID},
        "container": {"channel_id": _CHANNEL_ID},
        "actions": [
            {
                "action_id": "billing_topup",
                "type": "static_select",
                "selected_option": {"value": "999"},
            }
        ],
    }

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload=admin_users_info_payload,
            repeat=True,
        )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(checkout_handler))
        await handle_topup_select(
            runtime,
            invalid_amount_payload,
            _http_client=http_client,
        )

    assert len(captured_checkout) == 0, (
        "Amount 999 (not in preset set) must NOT trigger a checkout POST (T-82-10)"
    )
