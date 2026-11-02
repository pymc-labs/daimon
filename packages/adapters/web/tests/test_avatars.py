from __future__ import annotations

from daimon.core.stores.agent_avatars import get_or_create_avatar
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import WebClientFactory


async def test_a_face_is_served_from_the_apps_own_origin(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        face = await get_or_create_avatar(db, tenant_id=tenant.id, agent_name="helper")
    path = f"/avatars/{face.token}/{face.sha256[:12]}.png"
    async with web_client(sessionmaker=db_session_factory) as client:
        found = await client.get(path)
        small = await client.get(path, params={"size": 128})
        odd_size = await client.get(path, params={"size": 7})
        wrong_hash = await client.get(f"/avatars/{face.token}/000000000000.png")
        wrong_token = await client.get(f"/avatars/nope/{face.sha256[:12]}.png")

    assert found.status_code == 200 and found.content == face.png
    assert found.headers["content-type"] == "image/png"
    assert "immutable" in found.headers["cache-control"], "a face never changes under its hash"
    assert small.status_code == 200
    assert odd_size.status_code == 400, "only the sizes that are stored"
    assert (wrong_hash.status_code, wrong_token.status_code) == (404, 404), "the path is the key"
