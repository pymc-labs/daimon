"""Public avatar URLs serve current and first-use initials content hashes."""

from __future__ import annotations

import hashlib

import pytest
from daimon.adapters.mcp.server import _build_avatar_route
from daimon.core.stores.agent_avatars import (
    generate_default_png,
    get_or_create_avatar,
    reset_avatar,
)
from daimon.testing.factories import make_tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
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
        for size in (128, 512):
            sized = await client.get(f"{path}?size={size}")
            assert sized.status_code == 200
            assert "immutable" in sized.headers["cache-control"]
            from io import BytesIO

            from PIL import Image

            assert Image.open(BytesIO(sized.content)).size == (size, size)
        assert (await client.get(f"{path}?size=1024")).status_code == 400
        assert (await client.get(f"/avatars/{row.token}/bad.png")).status_code == 404
        async with db_session_factory.begin() as session:
            await reset_avatar(session, tenant_id=tenant.id, agent_name="Ada")
        assert (await client.get(path)).status_code == 404


@pytest.mark.asyncio
async def test_face_avatar_route_serves_stored_512_and_resized_128(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from io import BytesIO

    from PIL import Image

    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        row = await get_or_create_avatar(
            session, tenant_id=tenant.id, agent_name="Analyst", face_enabled=True
        )
    app = Starlette(
        routes=[Route("/avatars/{token}/{sha12}.png", _build_avatar_route(db_session_factory))]
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://example.test"
    ) as client:
        path = f"/avatars/{row.token}/{row.sha256[:12]}.png"
        original = await client.get(path)
        small = await client.get(f"{path}?size=128")
        assert original.content == row.png
        assert Image.open(BytesIO(original.content)).size == (512, 512)
        assert Image.open(BytesIO(small.content)).size == (128, 128)


@pytest.mark.asyncio
async def test_first_face_keeps_prior_initials_url_until_admin_change(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        initials = await get_or_create_avatar(session, tenant_id=tenant.id, agent_name="Analyst")
        await session.execute(
            text(
                "UPDATE agent_avatars SET png_128 = NULL, png_512 = NULL "
                "WHERE tenant_id = :tenant AND agent_name = 'analyst'"
            ),
            {"tenant": tenant.id},
        )
    async with db_session_factory.begin() as session:
        face = await get_or_create_avatar(
            session, tenant_id=tenant.id, agent_name="Analyst", face_enabled=True
        )
    assert face.token == initials.token
    assert face.sha256 != initials.sha256
    app = Starlette(
        routes=[Route("/avatars/{token}/{sha12}.png", _build_avatar_route(db_session_factory))]
    )
    old_path = f"/avatars/{initials.token}/{initials.sha256[:12]}.png"
    new_path = f"/avatars/{face.token}/{face.sha256[:12]}.png"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://example.test"
    ) as client:
        assert (await client.get(old_path)).content == initials.png
        assert (await client.get(f"{old_path}?size=128")).content == initials.png
        assert (await client.get(new_path)).content == face.png
        async with db_session_factory.begin() as session:
            await reset_avatar(
                session, tenant_id=tenant.id, agent_name="Analyst", face_enabled=True
            )
        assert (await client.get(old_path)).status_code == 404
        assert (await client.get(new_path)).status_code == 404


@pytest.mark.asyncio
async def test_avatar_route_serves_legacy_insert_without_pre_rendered_sizes(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    png = generate_default_png("Legacy")
    sha = hashlib.sha256(png).hexdigest()
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        await session.execute(
            text(
                "INSERT INTO agent_avatars (tenant_id, agent_name, token, sha256, png, source) "
                "VALUES (:tenant, 'legacy', 'legacy-token', :sha, :png, 'default')"
            ),
            {"tenant": tenant.id, "sha": sha, "png": png},
        )
    app = Starlette(
        routes=[Route("/avatars/{token}/{sha12}.png", _build_avatar_route(db_session_factory))]
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://example.test"
    ) as client:
        path = f"/avatars/legacy-token/{sha[:12]}.png"
        assert (await client.get(path)).content == png
        assert (await client.get(f"{path}?size=128")).content == png
        assert (await client.get(f"{path}?size=512")).content == png
