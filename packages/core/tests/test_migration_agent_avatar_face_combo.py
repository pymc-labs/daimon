"""The face migration backfills sizes for existing initials avatars."""

import importlib.util
from io import BytesIO
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import AgentAvatar
from daimon.core.stores.agent_avatars import generate_default_png
from daimon.testing.factories import make_tenant
from PIL import Image
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0063_agent_avatar_face_combo.py"
    spec = importlib.util.spec_from_file_location("migration_agent_avatar_face_combo", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.fresh_schema
async def test_existing_avatar_backfills_and_matches_orm(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    png = generate_default_png("Research")
    migration = _migration()
    connection = await db_session.connection()

    def run(sync_connection: Connection, *, upgrade: bool) -> None:
        with Operations.context(MigrationContext.configure(sync_connection)):
            (migration.upgrade if upgrade else migration.downgrade)()

    await connection.run_sync(lambda conn: run(conn, upgrade=False))
    await db_session.execute(
        text(
            "INSERT INTO agent_avatars (tenant_id, agent_name, token, sha256, png, source) "
            "VALUES (:tenant, 'research', 'old-token', 'old-hash', :png, 'default')"
        ),
        {"tenant": tenant.id, "png": png},
    )
    await connection.run_sync(lambda conn: run(conn, upgrade=True))
    avatar = await db_session.get(AgentAvatar, (tenant.id, "research"))
    assert avatar is not None
    assert avatar.png == png
    assert avatar.face_combo is None
    assert avatar.png_128 is not None and avatar.png_512 is not None
    assert Image.open(BytesIO(avatar.png_128)).size == (128, 128)
    assert Image.open(BytesIO(avatar.png_512)).size == (512, 512)
    await db_session.execute(
        text(
            "INSERT INTO agent_avatars (tenant_id, agent_name, token, sha256, png, source) "
            "VALUES (:tenant, 'legacy-worker', 'legacy-token', 'legacy-hash', :png, 'default')"
        ),
        {"tenant": tenant.id, "png": png},
    )
    legacy = await db_session.get(AgentAvatar, (tenant.id, "legacy-worker"))
    assert legacy is not None and legacy.png_128 is None and legacy.png_512 is None
