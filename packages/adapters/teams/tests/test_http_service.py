"""Ingress contract: health routes, the disabled gate, the body cap, and
SDK ownership of ``/api/messages``.

Only the outbound Bot Framework transport is faked (``TeamsApiFake``);
everything inbound is the real FastAPI + real Microsoft SDK route.
"""

from __future__ import annotations

from typing import Any

import httpx
import jwt
import pytest
from daimon.adapters.teams.http_service import (
    MAX_TEAMS_HTTP_BODY_BYTES,
    TeamsHttpService,
    bot_client,
    create_teams_http_service,
)
from daimon.core.teams_bot_framework import SERVICE_URL
from daimon.testing.asgi import asgi_lifespan
from microsoft_teams.api.auth.cloud_environment import PUBLIC
from microsoft_teams.common.http import MiddlewareContext, MiddlewareNext
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    TeamsApiFake,
    build_teams_client,
    build_teams_runtime,
    make_message_activity,
    teams_settings,
)


def _service(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    enabled: bool = True,
    fake: TeamsApiFake | None = None,
) -> TeamsHttpService:
    settings = teams_settings(enabled=enabled)
    return create_teams_http_service(
        settings=settings,
        runtime=build_teams_runtime(db_factory, teams=settings),
        client=build_teams_client(fake or TeamsApiFake()),
    )


async def _request(
    service: TeamsHttpService, method: str, path: str, **kwargs: Any
) -> httpx.Response:
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


async def test_healthz_is_live_without_lifespan(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await _request(_service(db_session_factory), "GET", "/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "live"


async def test_readyz_is_503_before_sdk_init_and_ready_after(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
) -> None:
    service = _service(db_session_factory)
    before = await _request(service, "GET", "/readyz")
    assert before.status_code == 503 and not service.ready
    async with asgi_lifespan(service.app):
        during = await _request(service, "GET", "/readyz")
        assert during.status_code == 200 and service.ready
        assert during.json()["status"] == "ready"


async def test_disabled_ingress_returns_503_while_health_stays_live(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
) -> None:
    service = _service(db_session_factory, enabled=False)
    async with asgi_lifespan(service.app):
        response = await _request(service, "POST", "/api/messages", json=make_message_activity())
        assert response.status_code == 503
        health = await _request(service, "GET", "/healthz")
        assert health.status_code == 200


async def test_oversized_body_is_413_before_sdk_parsing(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
) -> None:
    service = _service(db_session_factory)
    async with asgi_lifespan(service.app):
        oversize = b"x" * (MAX_TEAMS_HTTP_BODY_BYTES + 1)
        response = await _request(service, "POST", "/api/messages", content=oversize)
    assert response.status_code == 413


async def test_api_messages_is_sdk_owned_and_authenticated(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
    teams_api_fake: TeamsApiFake,
) -> None:
    """A valid personal-chat activity reaches the SDK route, not a hand-rolled endpoint."""
    service = _service(db_session_factory, fake=teams_api_fake)
    async with asgi_lifespan(service.app):
        response = await _request(service, "POST", "/api/messages", json=make_message_activity())
    assert response.status_code in (200, 201, 202), response.text


@pytest.mark.parametrize(
    ("issuer", "refused"),
    [
        ("https://login.microsoftonline.com/any-tenant/v2.0", True),
        ("https://sts.windows.net/t/", True),
        ("https://api.botframework.com", False),
    ],
)
async def test_only_a_bot_framework_token_reaches_the_sdk(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
    issuer: str,
    refused: bool,
) -> None:
    """The SDK would take any tenant's Entra token and skip the serviceUrl check."""
    token = jwt.encode({"iss": issuer, "aud": "app-id"}, "k" * 32, algorithm="HS256")
    service = _service(db_session_factory)
    async with asgi_lifespan(service.app):
        response = await _request(
            service,
            "POST",
            "/api/messages",
            json=make_message_activity(),
            headers={"Authorization": f"Bearer {token}"},
        )
    assert (response.status_code == 401) is refused, response.text


async def test_unauthenticated_request_is_rejected_by_sdk(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_bot_token: None,
) -> None:
    """Without the dev env var the SDK's own JWT gate answers 401."""
    service = _service(db_session_factory)
    async with asgi_lifespan(service.app):
        response = await _request(service, "POST", "/api/messages", json=make_message_activity())
    assert response.status_code == 401


async def test_the_service_retries_a_throttled_send_through_the_sdk(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_bot_token: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below every SDK middleware, so the production client chain is what retries."""
    settings = teams_settings()
    service = create_teams_http_service(
        settings=settings, runtime=build_teams_runtime(db_session_factory, teams=settings)
    )
    statuses = iter([429, 201])

    async def _send(self: httpx.AsyncClient, request: httpx.Request, **_: Any) -> httpx.Response:
        headers = {"Retry-After": "0"}
        return httpx.Response(next(statuses), headers=headers, json={"id": "m-1"}, request=request)

    async with asgi_lifespan(service.app):
        monkeypatch.setattr(httpx.AsyncClient, "send", _send)
        sent = await service.teams_app.send("a:conversation-1", "hi")
    assert sent.id == "m-1"
    assert next(statuses, None) is None, "one 429, then the retry"


async def test_a_throttled_bot_framework_call_is_retried_once() -> None:
    calls: list[str] = []

    class _ThrottleOnce:
        async def send(self, context: MiddlewareContext, next: MiddlewareNext) -> httpx.Response:
            request = httpx.Request(context.method, context.url)
            calls.append(context.method)
            if len(calls) == 1:
                response = httpx.Response(429, headers={"Retry-After": "0"}, request=request)
                raise httpx.HTTPStatusError("429", request=request, response=response)
            return httpx.Response(200, json={"id": "m-1"}, request=request)

    client = bot_client()
    client.use(_ThrottleOnce())
    response = await client.post(f"{SERVICE_URL}/v3/conversations/a/activities", json={})
    assert response.json() == {"id": "m-1"} and calls == ["POST", "POST"]


def test_proactive_sends_use_the_commercial_cloud(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(db_session_factory)
    options = service.teams_app.options
    assert options.service_url == SERVICE_URL and options.cloud is PUBLIC
