"""A new agent's face is rendered when it is created, not on its first post."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Mapping
from typing import cast
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.core import agent_fork, agent_identity
from daimon.core.agent_fork import copy_agent
from daimon.core.agent_identity import ensure_agent_face, queue_agent_face
from daimon.core.authz import Subject
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.stores.agent_avatars import get_agent_avatar
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

# A user's agent: not managed, and not the deployment default.
_PLAIN = {"metadata": None, "default_agent_name": "daimon"}


def _factory() -> async_sessionmaker[AsyncSession]:
    factory = MagicMock()
    factory.kw = {"bind": None}
    return cast(async_sessionmaker[AsyncSession], factory)


@pytest.mark.asyncio
async def test_ensure_agent_face_waits_for_the_render(monkeypatch: pytest.MonkeyPatch) -> None:
    rendered: list[str] = []

    async def generate(*_args: object, agent_name: str, **_kwargs: object) -> None:
        await asyncio.sleep(0.01)
        rendered.append(agent_name)

    monkeypatch.setattr(agent_identity, "get_or_create_avatar", generate)
    stored = await ensure_agent_face(
        _factory(), tenant_id=uuid.uuid4(), agent_name="Atlas Birch", timeout_s=5, **_PLAIN
    )
    assert stored and rendered == ["Atlas Birch"]


@pytest.mark.asyncio
async def test_ensure_agent_face_gives_up_at_its_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    release = asyncio.Event()

    async def generate(*_args: object, **_kwargs: object) -> None:
        await release.wait()

    monkeypatch.setattr(agent_identity, "get_or_create_avatar", generate)
    stored = await asyncio.wait_for(
        ensure_agent_face(
            _factory(), tenant_id=uuid.uuid4(), agent_name="Slow", timeout_s=0.01, **_PLAIN
        ),
        timeout=1,
    )
    assert not stored
    release.set()


@pytest.mark.asyncio
async def test_a_failed_render_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def generate(*_args: object, **_kwargs: object) -> None:
        raise OSError("missing face layer")

    monkeypatch.setattr(agent_identity, "get_or_create_avatar", generate)
    tenant_id = uuid.uuid4()
    stored = await ensure_agent_face(
        _factory(), tenant_id=tenant_id, agent_name="Broken", timeout_s=5, **_PLAIN
    )
    assert not stored
    # The failure takes the same back-off a first post's render would.
    assert agent_identity._face_failures[tenant_id, "broken"][0] == 1


def test_queue_agent_face_never_raises() -> None:
    factory = MagicMock()
    factory.kw = None  # `.kw.get` raises before any task starts
    task = queue_agent_face(
        cast(async_sessionmaker[AsyncSession], factory),
        tenant_id=uuid.uuid4(),
        agent_name="X",
        **_PLAIN,
    )
    assert task is None


@pytest.mark.asyncio
async def test_copy_agent_stores_the_copys_face(
    db_engine: AsyncEngine, db_clean: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del db_clean
    queued: list[str] = []

    def queue(
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        tenant_id: uuid.UUID,
        agent_name: str,
        metadata: Mapping[str, str] | None,
        default_agent_name: str | None,
    ) -> asyncio.Task[None] | None:
        queued.append(agent_name)
        return queue_agent_face(
            sessionmaker,
            tenant_id=tenant_id,
            agent_name=agent_name,
            metadata=metadata,
            default_agent_name=default_agent_name,
        )

    monkeypatch.setattr(agent_fork, "queue_agent_face", queue)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory.begin() as session:
        tenant = await make_tenant(session)
    source = ma_agent(id="ag_src", name="source", tenant_id=tenant.id)

    def on_create(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = ma_agent(id="ag_new", name="Atlas Birch", tenant_id=tenant.id)
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(200, json=source.model_dump(mode="json")),
    )
    router.add("POST", r"/v1/agents", on_create)
    await copy_agent(
        build_fake_anthropic(router.dispatch),
        factory,
        tenant_id=tenant.id,
        source=source,
        new_name="Atlas Birch",
        public_url=None,
        subject=Subject(is_admin=True),
        default_agent_name="daimon",
    )
    assert queued == ["Atlas Birch"]
    # copy_agent returns before the render finishes; the CLI awaits it like this.
    assert await ensure_agent_face(
        factory, tenant_id=tenant.id, agent_name="Atlas Birch", timeout_s=30, **_PLAIN
    )
    async with factory() as session:
        avatar = await get_agent_avatar(session, tenant_id=tenant.id, agent_name="Atlas Birch")
    assert avatar is not None and avatar.has_face_assignment


@pytest.mark.parametrize(
    ("name", "metadata"),
    [("Helper", {MA_METADATA_KEY_MANAGED: "true"}), ("daimon", None)],
    ids=["managed", "deployment-default"],
)
@pytest.mark.asyncio
async def test_a_built_in_agent_gets_no_face(
    monkeypatch: pytest.MonkeyPatch, name: str, metadata: dict[str, str] | None
) -> None:
    """A built-in agent posts with the platform app's icon, so nothing is rendered."""
    rendered: list[str] = []

    async def generate(*_args: object, agent_name: str, **_kwargs: object) -> None:
        rendered.append(agent_name)

    monkeypatch.setattr(agent_identity, "get_or_create_avatar", generate)
    tenant_id = uuid.uuid4()
    task = queue_agent_face(
        _factory(),
        tenant_id=tenant_id,
        agent_name=name,
        metadata=metadata,
        default_agent_name="daimon",
    )
    stored = await ensure_agent_face(
        _factory(),
        tenant_id=tenant_id,
        agent_name=name,
        metadata=metadata,
        default_agent_name="daimon",
        timeout_s=5,
    )
    assert task is None and not stored
    assert rendered == [] and not agent_identity._face_tasks
