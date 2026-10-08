"""Turn identity reads must remain independent of face rendering."""

from __future__ import annotations

import asyncio
import uuid
from typing import cast
from unittest.mock import MagicMock

import pytest
from daimon.core import agent_identity
from daimon.core.agent_identity import resolve_agent_identity
from daimon.core.stores.agent_avatars import AvatarLink
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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
