"""Public avatar URLs serve only the current content hash."""

from __future__ import annotations

import pytest
from daimon.adapters.mcp.server import _build_avatar_route
from daimon.core.stores.agent_avatars import get_or_create_avatar, reset_avatar
from daimon.testing.factories import make_tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route


@pytest.mark.asyncio
async def test_avatar_route_is_public_immutable_and_revokes_old_token(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        row = await get_or_create_avatar(session, tenant_id=tenant.id, agent_name="Ada")
    app = Starlette(
        routes=[Route("/avatars/{token}/{sha12}.png", _build_avatar_route(db_session_factory))]
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://example.test"
    ) as client:
        path = f"/avatars/{row.token}/{row.sha256[:12]}.png"
        response = await client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert "immutable" in response.headers["cache-control"]
        assert response.content == row.png
        assert (await client.get(f"/avatars/{row.token}/bad.png")).status_code == 404
        async with db_session_factory.begin() as session:
            await reset_avatar(session, tenant_id=tenant.id, agent_name="Ada")
        assert (await client.get(path)).status_code == 404
