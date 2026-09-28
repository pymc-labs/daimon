"""The `billing` panel, driven through the real SDK route: member and admin views, top-ups.

Only the outbound Bot Framework transport and the MCP checkout hop are faked.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from daimon.adapters.teams.billing_panel import ADMIN_ONLY, NOT_CONFIGURED, UNKNOWN_AMOUNT
from daimon.adapters.teams.http_service import TeamsHttpService, create_teams_http_service
from daimon.adapters.teams.identity import DENIED
from daimon.core.defaults.provisioning import provision_tenant
from daimon.testing.asgi import asgi_lifespan
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    BOT_ACCOUNT_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
    TeamsApiFake,
    build_teams_client,
    build_teams_runtime,
    make_message_activity,
    teams_settings,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")
CHECKOUT_URL = "https://checkout.example/abc"


@pytest.fixture(autouse=True)
async def provisioned_tenant(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)


@asynccontextmanager
async def _running(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    mcp: httpx.MockTransport | None = None,
) -> AsyncIterator[TeamsHttpService]:
    """An admin (AAD_OBJECT_ID) and a member (OTHER_AAD_OBJECT_ID); `mcp` fakes checkout."""
    settings = teams_settings(admins=(AAD_OBJECT_ID,))
    client = None if mcp is None else httpx.AsyncClient(transport=mcp)
    runtime = build_teams_runtime(db_factory, teams=settings, http_client=client)
    runtime.settings.mcp.app_root_url = "https://mcp.example"
    runtime.settings.mcp.jwt_secret = SecretStr("test-jwt-secret-at-least-32-chars-long!!")
    service = create_teams_http_service(
        settings=settings, runtime=runtime, client=build_teams_client(fake)
    )
    async with asgi_lifespan(service.app):
        await service.turns.start()
        yield service


async def _post(service: TeamsHttpService, payload: dict[str, object]) -> Any:
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/messages", json=payload)
    assert response.status_code == 200, response.text
    return response.json() if response.content else None


async def _command(service: TeamsHttpService, fake: TeamsApiFake, user: str) -> str:
    """Send `billing` as `user` and return the card it replies with."""
    seen = len(fake.activity_requests)
    activity = make_message_activity(text="billing", activity_id=f"a-{seen}", aad_object_id=user)
    await _post(service, activity)
    async with asyncio.timeout(10):
        while len(fake.activity_requests) == seen:
            await asyncio.sleep(0.01)
    return json.dumps(fake.activity_requests[-1].body, ensure_ascii=False)


def _click(op: str, *, user: str = AAD_OBJECT_ID, **extra: str) -> dict[str, object]:
    data: dict[str, object] = {"action": "billing", "op": op} | extra
    return {
        "type": "invoke",
        "name": "adaptiveCard/action",
        "id": f"invoke-{uuid.uuid4()}",
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "from": {"id": f"29:{user}", "aadObjectId": user},
        "recipient": {"id": BOT_ACCOUNT_ID, "name": "daimon"},
        "conversation": {
            "id": CONVERSATION_ID,
            "conversationType": "personal",
            "tenantId": ENTRA_TENANT_ID,
        },
        "value": {"action": {"type": "Action.Execute", "verb": "billing", "data": data}},
    }


@pytest.mark.asyncio
async def test_only_the_admin_view_offers_top_ups(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        member = await _command(service, teams_api_fake, OTHER_AAD_OBJECT_ID)
        admin = await _command(service, teams_api_fake, AAD_OBJECT_ID)

    assert "top-ups are admin-only" in member and "topup" not in member, "members only look"
    assert admin.count('"op": "topup"') == 4, "an admin gets one button per amount"
    assert "admin view" in admin


@pytest.mark.asyncio
async def test_top_up_clicks_recheck_admin_and_the_amount(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    posts: list[httpx.Request] = []
    mcp = httpx.MockTransport(lambda r: posts.append(r) or httpx.Response(500))
    async with _running(db_session_factory, teams_api_fake, mcp) as service:
        member = await _post(service, _click("topup", user=OTHER_AAD_OBJECT_ID, amount="25"))
        odd = await _post(service, _click("topup", amount="7"))
        stranger = _click("topup", amount="25")
        stranger["conversation"] = {"id": CONVERSATION_ID, "tenantId": str(uuid.UUID(int=99))}
        denied = await _post(service, stranger)

    assert member["value"] == ADMIN_ONLY, "a forwarded admin card does nothing for a member"
    assert odd["value"] == UNKNOWN_AMOUNT, "only the offered amounts are accepted"
    assert denied["value"] == DENIED, "an unverified clicker sees nothing"
    assert posts == [], "no checkout was created"


@pytest.mark.asyncio
async def test_an_admin_top_up_links_to_checkout(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    posts: list[httpx.Request] = []

    def checkout(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(200, json={"url": CHECKOUT_URL})

    async with _running(db_session_factory, teams_api_fake, httpx.MockTransport(checkout)) as svc:
        response = await _post(svc, _click("topup", amount="25"))

    [post] = posts
    assert str(post.url) == "https://mcp.example/billing/checkout"
    assert json.loads(post.content) == {"amount": 25}
    card = json.dumps(response)
    assert "Action.OpenUrl" in card and CHECKOUT_URL in card, "payment opens as a link"


@pytest.mark.asyncio
async def test_a_top_up_without_payments_says_so(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        response = await _post(service, _click("topup", amount="10"))

    assert NOT_CONFIGURED in json.dumps(response), "no billing routes mounted is not an error"
