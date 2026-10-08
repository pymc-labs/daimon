"""Avatar generation, URL resolution and tenant-scoped persistence."""

from __future__ import annotations

import pytest
from daimon.core.agent_identity import is_builtin_agent, resolve_agent_identity
from daimon.core.stores.agent_avatars import (
    delete_avatar,
    generate_default_png,
    get_avatar_by_token,
    get_or_create_avatar,
    normalize_agent_name,
    reset_avatar,
)
from daimon.core.stores.scoped_config_write import clear_agent_references
from daimon.testing.factories import make_tenant
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def test_default_avatar_is_stable_png() -> None:
    from io import BytesIO

    png = generate_default_png("Ada Lovelace")
    assert png == generate_default_png("Ada Lovelace")
    assert len(png) <= 256 * 1024
    with Image.open(BytesIO(png)) as image:
        assert image.format == "PNG"
        assert image.size == (256, 256)


def test_builtin_agent_uses_metadata_or_deployment_default() -> None:
    assert is_builtin_agent(
        name="Helper", metadata={"daimon_managed": "true"}, default_agent_name=None
    )
    assert is_builtin_agent(name="Main", metadata={}, default_agent_name="Main")
    assert not is_builtin_agent(name="Daimon", metadata={}, default_agent_name="Main")


def test_default_avatar_handles_non_ascii_and_expanding_uppercase() -> None:
    from io import BytesIO

    for name in ("ßeta", "𐐨 agent", "💬 agent"):
        with Image.open(BytesIO(generate_default_png(name))) as image:
            assert image.size == (256, 256)


@pytest.mark.asyncio
async def test_avatar_lifecycle_and_resolver(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    built_in = await resolve_agent_identity(
        db_session,
        tenant_id=tenant.id,
        agent_name="Daimon",
        is_builtin=True,
        public_base_url="https://example.test",
    )
    assert built_in.builtin and built_in.avatar_url is None

    pending = await resolve_agent_identity(
        db_session,
        tenant_id=tenant.id,
        agent_name="ＡＤＡ",
        is_builtin=False,
        public_base_url="https://example.test/",
        enabled=True,
    )
    assert pending.avatar_url is None
    row = await get_or_create_avatar(db_session, tenant_id=tenant.id, agent_name="ada")
    identity = await resolve_agent_identity(
        db_session,
        tenant_id=tenant.id,
        agent_name="ＡＤＡ",
        is_builtin=False,
        public_base_url="https://example.test/",
        enabled=True,
    )
    assert identity.name == "ＡＤＡ"
    assert normalize_agent_name("ＡＤＡ") == "ada"
    assert identity.avatar_url == f"https://example.test/avatars/{row.token}/{row.sha256[:12]}.png"
    assert (
        await get_or_create_avatar(db_session, tenant_id=tenant.id, agent_name="Ada")
    ).token == row.token

    reset = await reset_avatar(db_session, tenant_id=tenant.id, agent_name="Ada")
    assert reset.token != row.token
    assert await get_avatar_by_token(db_session, token=row.token) is None
    assert (
        await get_or_create_avatar(db_session, tenant_id=tenant.id, agent_name="Ada")
    ).token == reset.token
    await delete_avatar(db_session, tenant_id=tenant.id, agent_name="Ada")
    assert await get_avatar_by_token(db_session, token=reset.token) is None
    assert (
        await get_or_create_avatar(db_session, tenant_id=tenant.id, agent_name="Ada")
    ).token != reset.token


@pytest.mark.asyncio
async def test_disabled_identity_is_builtin_style_and_creates_no_avatar(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    identity = await resolve_agent_identity(
        db_session,
        tenant_id=tenant.id,
        agent_name="Research",
        is_builtin=False,
        public_base_url="https://example.test",
        enabled=False,
    )
    assert identity.name == "Research"
    assert identity.builtin
    assert identity.avatar_url is None
    count = await db_session.scalar(
        text("SELECT count(*) FROM agent_avatars WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant.id},
    )
    assert count == 0


@pytest.mark.asyncio
async def test_archive_cleanup_deletes_avatar(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    row = await get_or_create_avatar(db_session, tenant_id=tenant.id, agent_name="Ada")
    await clear_agent_references(db_session, tenant_id=tenant.id, agent_name="Ada")
    assert await get_avatar_by_token(db_session, token=row.token) is None


@pytest.mark.asyncio
async def test_avatar_url_absent_without_public_base(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    identity = await resolve_agent_identity(
        db_session,
        tenant_id=tenant.id,
        agent_name="Helper",
        is_builtin=False,
        public_base_url=None,
        enabled=True,
    )
    assert identity.avatar_url is None
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM agent_avatars WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant.id},
        )
        == 0
    )


@pytest.mark.asyncio
async def test_invalid_stored_face_keeps_png_url_and_does_not_block_other_agents(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    row = await get_or_create_avatar(db_session, tenant_id=tenant.id, agent_name="Analyst")
    await db_session.execute(
        text(
            "UPDATE agent_avatars SET face_combo = :bad "
            "WHERE tenant_id = :tenant_id AND agent_name = 'analyst'"
        ),
        {"bad": '{"v":2,"variant":["unknown"]}', "tenant_id": tenant.id},
    )
    db_session.expire_all()
    identity = await resolve_agent_identity(
        db_session,
        tenant_id=tenant.id,
        agent_name="Analyst",
        is_builtin=False,
        public_base_url="https://example.test",
        enabled=True,
    )
    assert identity.avatar_url == f"https://example.test/avatars/{row.token}/{row.sha256[:12]}.png"
    other = await get_or_create_avatar(
        db_session, tenant_id=tenant.id, agent_name="Research", face_enabled=True
    )
    assert other.face_combo is not None
