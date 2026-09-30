"""The chat billing panels' shared figures: formatters, the snapshot read and the checkout hop."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import jwt as pyjwt
import pytest
from daimon.core.billing_panel import (
    create_checkout,
    estimate_turns,
    fmt_usd,
    load_billing_snapshot,
)
from daimon.core.errors import DaimonError
from daimon.core.stores import usage_events
from daimon.testing import ma_model_usage
from daimon.testing.factories import make_tenant
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

_CALLER_ID = "U_CALLER"
_OTHER_ID = "U_OTHER"
_SINCE = datetime(2025, 1, 1, tzinfo=UTC)
_CHECKOUT_URL = "https://checkout.example/abc"


async def _tenant_with_usage(session: AsyncSession) -> uuid.UUID:
    """A tenant where the other user out-spends the caller."""
    tenant = await make_tenant(session, platform="slack", workspace_id="T_BILLING_TEST")
    for user_id, tokens in ((_CALLER_ID, 1000), (_OTHER_ID, 10_000)):
        await usage_events.record(
            session,
            tenant_id=tenant.id,
            platform_user_id=user_id,
            managed_session_id=f"sess-{user_id}",
            model="claude-opus-4-7",
            model_usage=ma_model_usage(input_tokens=tokens, output_tokens=tokens),
            event_id=f"evt-{user_id}",
        )
    await session.commit()
    return tenant.id


def test_fmt_usd_formats_cents_and_thousands() -> None:
    assert fmt_usd(12.5) == "$12.50", "two decimal places"
    assert fmt_usd(0.0) == "$0.00", "zero still shows cents"
    assert fmt_usd(Decimal("1000")) == "$1,000.00", "Decimal gets a thousands separator"
    assert fmt_usd(2500.75) == "$2,500.75", "float gets a thousands separator"


def test_estimate_turns_uses_the_tenant_average_or_the_fallback() -> None:
    assert estimate_turns(10.0, guild_spend=0.0, guild_turns=0) == 100, "$0.10/turn fallback"
    assert estimate_turns(10.0, guild_spend=20.0, guild_turns=4) == 2, "$5/turn average"
    assert estimate_turns(10.0, guild_spend=0.0, guild_turns=5) == 100, (
        "zero spend with turns falls back rather than dividing by zero cost"
    )


async def test_load_billing_snapshot_member_reads_only_caller_data(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant_with_usage(db_session)
    state = await load_billing_snapshot(
        db_session, tenant_id=tenant_id, platform_user_id=_CALLER_ID, is_admin=False, since=_SINCE
    )

    assert state.is_admin is False, "a member gets the member snapshot"
    assert state.caller_spend > 0.0 and state.caller_turns == 1, "the caller's own usage"
    assert state.member_rows == (), "no per-member breakdown for a member"
    assert (state.guild_spend, state.guild_turns, state.guild_distinct_members) == (0.0, 0, 0), (
        "tenant totals are not read for a member"
    )


async def test_load_billing_snapshot_admin_sorts_members_by_spend(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant_with_usage(db_session)
    state = await load_billing_snapshot(
        db_session, tenant_id=tenant_id, platform_user_id=_CALLER_ID, is_admin=True, since=_SINCE
    )

    assert state.is_admin is True, "an admin gets the admin snapshot"
    assert [row.platform_user_id for row in state.member_rows] == [_OTHER_ID, _CALLER_ID], (
        "rows are ordered by spend, highest first"
    )
    assert state.member_rows[1].is_caller, "the caller's own row is flagged"
    assert state.guild_distinct_members == 2, "both spenders are counted"


async def test_load_billing_snapshot_admin_caps_at_25_members(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_BILLING_MANY")
    for i in range(30):
        await usage_events.record(
            db_session,
            tenant_id=tenant.id,
            platform_user_id=f"U_MANY_{i:03d}",
            managed_session_id=f"sess-many-{i}",
            model="claude-opus-4-7",
            model_usage=ma_model_usage(input_tokens=100 * (i + 1), output_tokens=50 * (i + 1)),
            event_id=f"evt-many-{i}",
        )
    await db_session.commit()

    state = await load_billing_snapshot(
        db_session, tenant_id=tenant.id, platform_user_id="U_MANY_000", is_admin=True, since=_SINCE
    )

    assert len(state.member_rows) == 25, "member rows are capped at 25"
    assert state.over_cap_count == 5, "the rows past the cap are counted"


async def test_load_billing_snapshot_empty_period_is_well_formed(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_BILLING_EMPTY")
    await db_session.commit()
    state = await load_billing_snapshot(
        db_session, tenant_id=tenant.id, platform_user_id="U_EMPTY", is_admin=False, since=_SINCE
    )

    assert (state.caller_spend, state.caller_turns, state.member_rows) == (0.0, 0, ()), (
        "an empty month reads as zeros, not an error"
    )


def _mcp_settings() -> MagicMock:
    settings = MagicMock()
    settings.app_root_url = "https://mcp.example.com"
    settings.jwt_secret = SecretStr("test-jwt-secret-at-least-32-chars-long!!")
    return settings


async def test_create_checkout_posts_only_the_amount_with_a_bearer_token() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"url": _CHECKOUT_URL})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        url = await create_checkout(
            client, settings=_mcp_settings(), account_id=uuid.uuid4(), amount=25
        )

    assert url == _CHECKOUT_URL, "the Checkout URL from the MCP answer"
    [request] = captured
    assert request.method == "POST" and request.url.path == "/billing/checkout"
    assert json.loads(request.content) == {"amount": 25}, "the tenant never rides in the body"
    assert request.headers["authorization"].startswith("Bearer "), "the hop is authenticated"


async def test_create_checkout_sends_a_plain_account_token() -> None:
    """The checkout route never checks admin, so the hop carries no admin or internal claim."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"url": _CHECKOUT_URL})

    account_id = uuid.uuid4()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await create_checkout(client, settings=_mcp_settings(), account_id=account_id, amount=10)

    token = captured[0].headers["authorization"].removeprefix("Bearer ")
    claims = pyjwt.decode(token, options={"verify_signature": False})
    assert claims["sub"] == str(account_id), "the verifier derives the tenant from the account"
    assert "internal" not in claims and "is_admin" not in claims, (
        "a checkout hop must not mint an internal admin bearer"
    )


async def test_create_checkout_raises_on_non_2xx() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "invalid amount"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await create_checkout(
                client, settings=_mcp_settings(), account_id=uuid.uuid4(), amount=999
            )


async def test_create_checkout_refuses_without_mcp_settings() -> None:
    settings = MagicMock()
    settings.app_root_url = None
    settings.jwt_secret = None
    async with httpx.AsyncClient() as client:
        with pytest.raises(DaimonError, match="DAIMON_MCP__PUBLIC_URL"):
            await create_checkout(client, settings=settings, account_id=uuid.uuid4(), amount=10)
