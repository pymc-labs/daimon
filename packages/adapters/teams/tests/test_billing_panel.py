"""The `billing` panel, driven through the real SDK route: member and admin views, top-ups.

Only the outbound Bot Framework transport and the MCP checkout hop are faked.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import AbstractAsyncContextManager

import httpx
import pytest
from daimon.adapters.teams.billing_panel import ADMIN_ONLY, NOT_CONFIGURED, UNKNOWN_AMOUNT
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.adapters.teams.identity import DENIED
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CONVERSATION_ID,
    OTHER_AAD_OBJECT_ID,
    TeamsApiFake,
    build_teams_runtime,
    make_card_action,
    make_message_activity,
    post_activity,
    running_service,
    teams_settings,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
CHECKOUT_URL = "https://checkout.example/abc"


def _running(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    mcp: httpx.MockTransport | None = None,
) -> AbstractAsyncContextManager[TeamsHttpService]:
    """An admin (AAD_OBJECT_ID) and a member (OTHER_AAD_OBJECT_ID); `mcp` fakes checkout."""
    settings = teams_settings(admins=(AAD_OBJECT_ID,))
    client = None if mcp is None else httpx.AsyncClient(transport=mcp)
    runtime = build_teams_runtime(db_factory, teams=settings, http_client=client)
    runtime.settings.mcp.app_root_url = "https://mcp.example"
    runtime.settings.mcp.jwt_secret = SecretStr("test-jwt-secret-at-least-32-chars-long!!")
    return running_service(runtime, fake)


async def _command(service: TeamsHttpService, fake: TeamsApiFake, user: str) -> str:
    """Send `billing` as `user` and return the card it replies with."""
    seen = len(fake.activity_requests)
    activity = make_message_activity(text="billing", activity_id=f"a-{seen}", aad_object_id=user)
    await post_activity(service, activity)
    async with asyncio.timeout(10):
        while len(fake.activity_requests) == seen:
            await asyncio.sleep(0.01)
    return json.dumps(fake.activity_requests[-1].body, ensure_ascii=False)


def _click(op: str, *, user: str = AAD_OBJECT_ID, **extra: str) -> dict[str, object]:
    return make_card_action("billing", op, user=user, **extra)


async def test_only_the_admin_view_offers_top_ups(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        member = await _command(service, teams_api_fake, OTHER_AAD_OBJECT_ID)
        admin = await _command(service, teams_api_fake, AAD_OBJECT_ID)

    assert "top-ups are admin-only" in member and "topup" not in member, "members only look"
    assert admin.count('"op": "topup"') == 4, "an admin gets one button per amount"
    assert "admin view" in admin


async def test_top_up_clicks_recheck_admin_and_the_amount(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    posts: list[httpx.Request] = []
    mcp = httpx.MockTransport(lambda r: posts.append(r) or httpx.Response(500))
    async with _running(db_session_factory, teams_api_fake, mcp) as service:
        member = await post_activity(
            service, _click("topup", user=OTHER_AAD_OBJECT_ID, amount="25")
        )
        odd = await post_activity(service, _click("topup", amount="7"))
        stranger = _click("topup", amount="25")
        stranger["conversation"] = {"id": CONVERSATION_ID, "tenantId": str(uuid.UUID(int=99))}
        denied = await post_activity(service, stranger)

    assert member["value"] == ADMIN_ONLY, "a forwarded admin card does nothing for a member"
    assert odd["value"] == UNKNOWN_AMOUNT, "only the offered amounts are accepted"
    assert denied["value"] == DENIED, "an unverified clicker sees nothing"
    assert posts == [], "no checkout was created"


async def test_an_admin_top_up_links_to_checkout(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    posts: list[httpx.Request] = []

    def checkout(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(200, json={"url": CHECKOUT_URL})

    async with _running(db_session_factory, teams_api_fake, httpx.MockTransport(checkout)) as svc:
        response = await post_activity(svc, _click("topup", amount="25"))

    [post] = posts
    assert str(post.url) == "https://mcp.example/billing/checkout"
    assert json.loads(post.content) == {"amount": 25}
    card = json.dumps(response)
    assert "Action.OpenUrl" in card and CHECKOUT_URL in card, "payment opens as a link"


async def test_a_top_up_without_payments_says_so(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        response = await post_activity(service, _click("topup", amount="10"))

    assert NOT_CONFIGURED in json.dumps(response), "no billing routes mounted is not an error"
