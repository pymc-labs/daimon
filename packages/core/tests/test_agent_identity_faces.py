"""Turn identity reads must remain independent of face rendering."""

from __future__ import annotations

import asyncio
import uuid
from typing import cast
from unittest.mock import MagicMock

import pytest
from daimon.core import agent_faces, agent_identity
from daimon.core.agent_identity import resolve_agent_identity
from daimon.core.stores.agent_avatars import AvatarLink, get_agent_avatar
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_missing_face_queues_generation_without_blocking_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid.uuid4()
    started = asyncio.Event()
    release = asyncio.Event()

    async def lookup(*_args: object, **_kwargs: object) -> None:
        return None

    async def generate(*_args: object, **_kwargs: object) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(agent_identity, "get_agent_avatar", lookup)
    monkeypatch.setattr(agent_identity, "get_or_create_avatar", generate)
    factory = MagicMock()
    factory.kw = {"bind": None}
    identity = await asyncio.wait_for(
        resolve_agent_identity(
            cast(AsyncSession, object()),
            tenant_id=tenant_id,
            agent_name="Analyst",
            is_builtin=False,
            public_base_url="https://example.test",
            enabled=True,
            background_sessionmaker=cast(async_sessionmaker[AsyncSession], factory),
        ),
        timeout=0.1,
    )
    assert identity.avatar_url is None
    await asyncio.wait_for(started.wait(), timeout=1)
    assert (tenant_id, "analyst") in agent_identity._face_tasks
    release.set()
    await asyncio.gather(*agent_identity._face_tasks.values())


@pytest.mark.asyncio
async def test_broken_face_lookup_returns_name_without_picture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lookup(*_args: object, **_kwargs: object) -> None:
        raise ValueError("bad stored face")

    monkeypatch.setattr(agent_identity, "get_agent_avatar", lookup)
    identity = await resolve_agent_identity(
        cast(AsyncSession, object()),
        tenant_id=uuid.uuid4(),
        agent_name="Analyst",
        is_builtin=False,
        public_base_url="https://example.test",
        enabled=True,
    )
    assert identity.name == "Analyst"
    assert identity.avatar_url is None
    assert not identity.builtin


async def test_switch_off_does_not_load_a_broken_face_catalogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_catalogue() -> None:
        raise ValueError("bad face manifest")

    monkeypatch.setattr(agent_faces, "_catalogue", broken_catalogue)
    identity = await resolve_agent_identity(
        cast(AsyncSession, object()),
        tenant_id=uuid.uuid4(),
        agent_name="Research",
        is_builtin=False,
        public_base_url="https://example.test",
        enabled=False,
    )
    assert identity.builtin and identity.avatar_url is None


@pytest.mark.asyncio
async def test_existing_initials_url_is_used_while_face_is_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid.uuid4()
    started = asyncio.Event()
    release = asyncio.Event()

    async def lookup(*_args: object, **_kwargs: object) -> AvatarLink:
        return AvatarLink("old-token", "123456789abc" + "0" * 52, "default", False)

    async def generate(*_args: object, **_kwargs: object) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(agent_identity, "get_agent_avatar", lookup)
    monkeypatch.setattr(agent_identity, "get_or_create_avatar", generate)
    factory = MagicMock()
    factory.kw = {"bind": None}
    identity = await resolve_agent_identity(
        cast(AsyncSession, object()),
        tenant_id=tenant_id,
        agent_name="Analyst",
        is_builtin=False,
        public_base_url="https://example.test",
        enabled=True,
        background_sessionmaker=cast(async_sessionmaker[AsyncSession], factory),
    )
    assert identity.avatar_url == "https://example.test/avatars/old-token/123456789abc.png"
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    await asyncio.gather(*agent_identity._face_tasks.values())


@pytest.mark.asyncio
async def test_face_generation_error_does_not_escape_background_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lookup(*_args: object, **_kwargs: object) -> None:
        return None

    async def generate(*_args: object, **_kwargs: object) -> None:
        raise OSError("missing face layer")

    monkeypatch.setattr(agent_identity, "get_agent_avatar", lookup)
    monkeypatch.setattr(agent_identity, "get_or_create_avatar", generate)
    factory = MagicMock()
    factory.kw = {"bind": None}
    identity = await resolve_agent_identity(
        cast(AsyncSession, object()),
        tenant_id=uuid.uuid4(),
        agent_name="Research",
        is_builtin=False,
        public_base_url="https://example.test",
        enabled=True,
        background_sessionmaker=cast(async_sessionmaker[AsyncSession], factory),
    )
    assert identity.name == "Research" and identity.avatar_url is None
    await asyncio.gather(*agent_identity._face_tasks.values())


@pytest.mark.asyncio
async def test_background_face_generation_uses_independent_database_sessions(
    db_engine: AsyncEngine, db_clean: None
) -> None:
    del db_clean
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory.begin() as session:
        tenant = await make_tenant(session)
    async with factory.begin() as session:
        identity = await resolve_agent_identity(
            session,
            tenant_id=tenant.id,
            agent_name="Background Research",
            is_builtin=False,
            public_base_url="https://example.test",
            enabled=True,
            background_sessionmaker=factory,
        )
    assert identity.avatar_url is None
    task = agent_identity._face_tasks.get((tenant.id, "background research"))
    if task is not None:
        await task
    async with factory() as session:
        avatar = await get_agent_avatar(
            session, tenant_id=tenant.id, agent_name="Background Research"
        )
    assert avatar is not None and avatar.has_face_assignment


@pytest.mark.asyncio
async def test_face_generation_backs_off_and_stops_after_three_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid.uuid4()
    key = tenant_id, "research"
    calls = 0

    async def lookup(*_args: object, **_kwargs: object) -> None:
        return None

    async def generate(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise OSError("missing face layer")

    monkeypatch.setattr(agent_identity, "get_agent_avatar", lookup)
    monkeypatch.setattr(agent_identity, "get_or_create_avatar", generate)
    factory = MagicMock()
    factory.kw = {"bind": None}
    for expected in range(1, 4):
        await resolve_agent_identity(
            cast(AsyncSession, object()),
            tenant_id=tenant_id,
            agent_name="Research",
            is_builtin=False,
            public_base_url=None,
            enabled=True,
            background_sessionmaker=cast(async_sessionmaker[AsyncSession], factory),
        )
        await agent_identity._face_tasks[key]
        await asyncio.sleep(0)
        assert calls == expected
        assert agent_identity._face_failures[key][0] == expected
        if expected < 3:
            await resolve_agent_identity(
                cast(AsyncSession, object()),
                tenant_id=tenant_id,
                agent_name="Research",
                is_builtin=False,
                public_base_url=None,
                enabled=True,
                background_sessionmaker=cast(async_sessionmaker[AsyncSession], factory),
            )
            assert key not in agent_identity._face_tasks
            agent_identity._face_failures[key] = expected, 0.0
    await resolve_agent_identity(
        cast(AsyncSession, object()),
        tenant_id=tenant_id,
        agent_name="Research",
        is_builtin=False,
        public_base_url=None,
        enabled=True,
        background_sessionmaker=cast(async_sessionmaker[AsyncSession], factory),
    )
    assert calls == 3
    agent_identity._face_failures.pop(key)
