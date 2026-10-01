"""Tests for billing_panel/actions.py (top-up select).

Covers (create_checkout itself is covered in core's test_billing_panel.py):
- handle_topup_select with an admin user + amount=25: asserts checkout POST and
  ephemeral chat.postEphemeral with the mrkdwn <url|...> link.
- handle_topup_select with a non-admin user: asserts NO checkout POST.
- handle_topup_select with an invalid amount: asserts NO checkout POST (T-82-10).

Transport-level fakes:
  - MCP /billing/checkout: httpx.MockTransport (injected via _http_client parameter)
  - Slack Web API: aioresponses (for resolve_is_admin users.info + chat.postEphemeral)

No stripe imports anywhere in billing_panel. No method-level AsyncMock.
"""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest_asyncio
import yarl
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
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
    )


def _build_admin_payload(*, amount: int) -> dict[str, Any]:
    """Block actions payload for billing_topup with admin user."""
    return {
        "team": {"id": _TEAM_ID},
        "user": {"id": _USER_ID},
        "container": {"channel_id": _CHANNEL_ID},
        "view": {"id": "V_BILLING_TEST", "hash": "H_BILLING_TEST"},
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
# Integration: handle_topup_select — non-admin user
# ---------------------------------------------------------------------------


async def test_handle_topup_select_non_admin_issues_no_checkout_post(
    runtime: SlackRuntime,
) -> None:
    """Non-admin click must NOT trigger a checkout POST (fail-closed)."""
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

    assert len(captured_checkout) == 0, "Non-admin must NOT trigger a checkout POST (fail-closed)"
    views_update_key = ("POST", yarl.URL(_VIEWS_UPDATE_URL))
    assert views_update_key not in mock.requests, (
        "Non-admin refusal must stay silent — no views.update either"
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


# ---------------------------------------------------------------------------
# Integration: handle_topup_select — checkout route missing (no payment provider)
# ---------------------------------------------------------------------------


async def test_handle_topup_select_updates_modal_when_checkout_route_is_missing(
    runtime: SlackRuntime,
) -> None:
    """When /billing/checkout 404s, the open modal is updated with a visible message
    instead of leaving the top-up select silently dead."""
    from daimon.adapters.slack.billing_panel.actions import handle_topup_select

    def checkout_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

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
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload=admin_users_info_payload,
            repeat=True,
        )
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            _VIEWS_UPDATE_URL,
            payload={"ok": True, "view": {"id": "V_BILLING_TEST", "hash": "H_BILLING_TEST"}},
            repeat=True,
        )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(checkout_handler))
        await handle_topup_select(
            runtime,
            _build_admin_payload(amount=25),
            _http_client=http_client,
        )

    views_update_key = ("POST", yarl.URL(_VIEWS_UPDATE_URL))
    update_calls = mock.requests.get(views_update_key)
    assert update_calls, "handle_topup_select must send a views.update when checkout 404s"
    assert len(update_calls) == 1, "exactly one views.update must be sent on checkout failure"
    body: dict[str, Any] = update_calls[0].kwargs["json"]
    view_text = str(body["view"]["blocks"][0]["text"]["text"])
    assert "aren't configured" in view_text, (
        "views.update body must tell the operator payments aren't configured"
    )
    assert "manual credit top-up" in view_text, (
        "views.update body must point the operator at a manual credit top-up"
    )


async def test_handle_topup_select_admin_success_sends_no_views_update(
    runtime: SlackRuntime,
) -> None:
    """A successful checkout must not touch the modal — the ephemeral link is the
    only response (mirroring it into the modal is out of scope)."""
    from daimon.adapters.slack.billing_panel.actions import handle_topup_select

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

    with AioResponsesMock() as mock:
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

    views_update_key = ("POST", yarl.URL(_VIEWS_UPDATE_URL))
    assert views_update_key not in mock.requests, (
        "a successful checkout must not send any views.update"
    )
