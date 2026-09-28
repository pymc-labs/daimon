"""Ingress contract: health routes, the disabled gate, the body cap, and
SDK ownership of ``/api/messages``.

Only the outbound Bot Framework transport is faked (``TeamsApiFake``);
everything inbound is the real FastAPI + real Microsoft SDK route.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from daimon.adapters.teams.http_service import (
    MAX_TEAMS_HTTP_BODY_BYTES,
    TeamsHttpService,
    create_teams_http_service,
)
from daimon.testing.asgi import asgi_lifespan
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


@pytest.mark.asyncio
async def test_healthz_is_live_without_lifespan(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await _request(_service(db_session_factory), "GET", "/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "live"


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_unauthenticated_request_is_rejected_by_sdk(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_bot_token: None,
) -> None:
    """Without the dev env var the SDK's own JWT gate answers 401."""
    service = _service(db_session_factory)
    async with asgi_lifespan(service.app):
        response = await _request(service, "POST", "/api/messages", json=make_message_activity())
    assert response.status_code == 401
