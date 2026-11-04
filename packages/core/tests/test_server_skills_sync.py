"""The server skill sync: gate, publish per provider, repin followers, survive failures."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from daimon.core._models import WebPerson
from daimon.core.mcp_skills.install import ServerSkill
from daimon.core.mcp_skills.manifest import SkillEntry, parse_entry
from daimon.core.server_skills.gate import AttachedAgent, manifest_digest, manifest_of
from daimon.core.server_skills.sync import (
    SyncDeps,
    TargetError,
    UpstreamGone,
    approve_update,
    check_mirror,
)
from daimon.core.skills.ingest import SkillBundle
from daimon.core.stores import server_skills as store
from daimon.core.stores.domain import ServerSkillRow
from daimon.testing.factories import make_tenant, make_web_person
from daimon.testing.mcp_skills import SkillsServer, skill_md
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 1, 1, tzinfo=UTC)
NAME = "notes"


@dataclass
class FakeUpstream:
    server: SkillsServer
    tools: Any = None
    gone: bool = False

    def current(self) -> SkillEntry:
        raw = self.server.entries()[0]
        if self.tools is not None:
            raw["frontmatter"]["allowed-tools"] = self.tools
        entry = parse_entry(raw)
        assert entry is not None
        return entry

    async def entry(self, mirror: ServerSkillRow) -> SkillEntry | None:
        if self.gone:
            raise UpstreamGone("refused")
        return self.current()

    async def fetch(self, mirror: ServerSkillRow) -> ServerSkill:
        return ServerSkill(entry=self.current(), files=dict(self.server.skills[NAME]))


@dataclass
class FakeTarget:
    provider: str
    agents: dict[str, str | None]
    failing: bool = False
    published: list[str] = field(default_factory=list[str])

    async def publish(self, mirror: ServerSkillRow, bundle: SkillBundle) -> str:
        if self.failing:
            raise TargetError("down")
        self.published.append(bundle.preview.name)
        return str(len(self.published) + 1)

    async def attached(self, mirror: ServerSkillRow) -> list[AttachedAgent]:
        return [AttachedAgent(agent, version) for agent, version in self.agents.items()]

    async def repin(self, mirror: ServerSkillRow, agent_id: str, version: str) -> None:
        if self.failing:
            raise TargetError("down")
        self.agents[agent_id] = version


@asynccontextmanager
async def _no_lock() -> AsyncIterator[None]:
    yield


@dataclass
class World:
    deps: SyncDeps
    mirror: ServerSkillRow
    upstream: FakeUpstream
    first: FakeTarget
    second: FakeTarget


async def _world(db_session: AsyncSession, factory: async_sessionmaker[AsyncSession]) -> World:
    server = SkillsServer(skills={NAME: {"SKILL.md": skill_md(NAME)}})
    upstream = FakeUpstream(server)
    tenant = await make_tenant(db_session)
    added_by = (await make_web_person(db_session, tenant=tenant)).id
    manifest = manifest_of(upstream.current())
    mirror = await store.create_mirror(
        db_session,
        tenant_id=tenant.id,
        server="data",
        uri=server.uri(NAME),
        name=NAME,
        skill_id="skill_1",
        version_label=None,
        digest=manifest_digest(manifest),
        manifest=manifest,
        allowed_tools=None,
        added_by_person_id=added_by,
        now=NOW,
    )
    first = FakeTarget("first", {"follows": "1", "pins": "1"})
    second = FakeTarget("second", {"other": "1"})
    for target in (first, second):
        await store.put_deployment(
            db_session,
            mirror_id=mirror.id,
            tenant_id=tenant.id,
            provider=target.provider,
            digest=mirror.digest,
            version="1",
            now=NOW,
        )
    await store.set_follow(
        db_session, tenant_id=tenant.id, agent_id="pins", skill_id="skill_1", follow=False, now=NOW
    )
    await db_session.flush()
    deps = SyncDeps(
        sessionmaker=factory,
        upstream=lambda _m: upstream,
        targets=(first, second),
        lock=lambda _m: _no_lock(),
        now=lambda: NOW,
    )
    return World(deps, mirror, upstream, first, second)


async def _row(world: World) -> ServerSkillRow:
    async with world.deps.sessionmaker() as session:
        row = await store.get_mirror(session, world.mirror.id)
    assert row is not None
    return row


async def _versions(world: World) -> dict[str, str]:
    async with world.deps.sessionmaker() as session:
        held = await store.list_deployments(session, world.mirror.id)
    return {provider: row.version for provider, row in held.items()}


async def test_unchanged_skill_publishes_nothing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world = await _world(db_session, db_session_factory)
    assert await check_mirror(world.deps, world.mirror.id) == "unchanged"
    assert world.first.published == [] and world.second.published == []


async def test_text_update_reaches_every_provider_but_pinned_agents(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world = await _world(db_session, db_session_factory)
    world.upstream.server.skills[NAME]["guide.md"] = b"More.\n"

    assert await check_mirror(world.deps, world.mirror.id) == "updated"

    assert await _versions(world) == {"first": "2", "second": "2"}
    assert world.first.agents == {"follows": "2", "pins": "1"}, "a pinned agent stays"
    assert world.second.agents == {"other": "2"}
    row = await _row(world)
    assert row.status == "current" and "guide.md" in row.manifest


async def test_a_failing_provider_keeps_its_agents_and_retries(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world = await _world(db_session, db_session_factory)
    world.upstream.server.skills[NAME]["guide.md"] = b"More.\n"
    world.second.failing = True

    assert await check_mirror(world.deps, world.mirror.id) == "failed"
    assert await _versions(world) == {"first": "2", "second": "1"}
    assert world.first.agents["follows"] == "2"
    assert world.second.agents == {"other": "1"}, "never pinned to a version it lacks"

    world.second.failing = False
    assert await check_mirror(world.deps, world.mirror.id) == "unchanged"
    assert await _versions(world) == {"first": "2", "second": "2"}
    assert world.first.published == [NAME], "the provider that held it is not republished"
    assert world.second.agents == {"other": "2"}


@pytest.mark.parametrize("change", ["script", "tools"])
async def test_risky_update_waits_for_approval(
    change: str,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session, db_session_factory)
    if change == "script":
        world.upstream.server.skills[NAME]["run.py"] = b"print(1)\n"
    else:
        world.upstream.tools = "Bash"
        world.upstream.server.skills[NAME]["SKILL.md"] = skill_md(NAME).replace(
            b"---\nDo", b"allowed-tools: Bash\n---\nDo"
        )

    assert await check_mirror(world.deps, world.mirror.id) == "pending"
    assert await check_mirror(world.deps, world.mirror.id) == "pending"
    row = await _row(world)
    assert row.status == "pending" and row.pending_digest is not None
    assert world.first.published == [] and world.first.agents["follows"] == "1"
    if change == "script":
        assert row.pending_changes == {"added": ["run.py"], "removed": [], "changed": []}

    assert await approve_update(world.deps, world.mirror.id) == "updated"
    row = await _row(world)
    assert row.status == "current" and row.pending_digest is None
    assert world.first.agents["follows"] == "2"


async def test_gone_skill_keeps_its_version_and_resumes(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world = await _world(db_session, db_session_factory)
    world.upstream.gone = True

    assert await check_mirror(world.deps, world.mirror.id) == "unavailable"
    assert (await _row(world)).status == "unavailable"
    assert world.first.agents["follows"] == "1"

    world.upstream.gone = False
    assert await check_mirror(world.deps, world.mirror.id) == "unchanged"
    assert (await _row(world)).status == "current"


async def test_skill_of_a_purged_person_is_unavailable(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world = await _world(db_session, db_session_factory)
    await db_session.execute(
        delete(WebPerson).where(WebPerson.id == world.mirror.added_by_person_id)
    )
    assert (await _row(world)).added_by_person_id is None
    assert await check_mirror(world.deps, world.mirror.id) == "unavailable"


async def test_unknown_mirror_fails(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world = await _world(db_session, db_session_factory)
    assert await check_mirror(world.deps, uuid.uuid4()) == "failed"
