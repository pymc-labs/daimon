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
    create_teams_http_service,
)
from daimon.core.config import TeamsSettings
from daimon.testing.asgi import asgi_lifespan
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    BOT_CLIENT_ID,
    ENTRA_TENANT_ID,
    TeamsApiFake,
    build_teams_client,
    build_teams_runtime,
    make_message_activity,
)


def _settings(*, enabled: bool = True) -> TeamsSettings:
    return TeamsSettings(
        client_id=BOT_CLIENT_ID,
        client_secret=SecretStr("test-secret"),
        tenant_id=ENTRA_TENANT_ID,
        port=3978,
        enabled=enabled,
    )


def _service(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    enabled: bool = True,
    fake: TeamsApiFake | None = None,
) -> Any:
    runtime = build_teams_runtime(db_factory)
    return create_teams_http_service(
        settings=_settings(enabled=enabled),
        runtime=runtime,
        client=build_teams_client(fake or TeamsApiFake()),
    )


async def _post(service_app: Any, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=service_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/messages", **kwargs)


@pytest.mark.asyncio
async def test_healthz_is_live_without_lifespan(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(db_session_factory)
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "live"


@pytest.mark.asyncio
async def test_readyz_is_503_before_sdk_init_and_ready_after(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
) -> None:
    service = _service(db_session_factory)
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        before = await client.get("/readyz")
        assert before.status_code == 503
        async with asgi_lifespan(service.app):
            during = await client.get("/readyz")
            assert during.status_code == 200
            assert during.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_disabled_ingress_returns_503_while_health_stays_live(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
) -> None:
    service = _service(db_session_factory, enabled=False)
    async with asgi_lifespan(service.app):
        response = await _post(service.app, json=make_message_activity())
        assert response.status_code == 503
        transport = httpx.ASGITransport(app=service.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/healthz")
        assert health.status_code == 200


@pytest.mark.asyncio
async def test_oversized_body_is_413_before_sdk_parsing(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
) -> None:
    service = _service(db_session_factory)
    async with asgi_lifespan(service.app):
        response = await _post(service.app, content=b"x" * (MAX_TEAMS_HTTP_BODY_BYTES + 1))
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_api_messages_is_sdk_owned_and_authenticated(
    db_session_factory: async_sessionmaker[AsyncSession],
    entra_env: None,
    stub_bot_token: None,
    teams_api_fake: TeamsApiFake,
) -> None:
    """A valid personal-chat activity reaches the SDK route — proof that
    /api/messages is the SDK's, not a hand-rolled endpoint."""
    service = _service(db_session_factory, fake=teams_api_fake)
    async with asgi_lifespan(service.app):
        response = await _post(service.app, json=make_message_activity())
    assert response.status_code in (200, 201, 202), response.text


@pytest.mark.asyncio
async def test_unauthenticated_request_is_rejected_by_sdk(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_bot_token: None,
) -> None:
    """Without the dev env var the SDK's own JWT gate answers 401 —
    ingress is authenticated by the SDK, not by us."""
    service = _service(db_session_factory)
    async with asgi_lifespan(service.app):
        response = await _post(service.app, json=make_message_activity())
    assert response.status_code == 401
