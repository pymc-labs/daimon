"""The server skill sync: gate, publish per provider, repin followers, survive failures."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.core._models import WebPerson
from daimon.core.mcp_skills.install import ServerSkill
from daimon.core.mcp_skills.manifest import SkillEntry, parse_entry
from daimon.core.server_skills import follows as follows_module
from daimon.core.server_skills.follows import set_agent_follow
from daimon.core.server_skills.gate import AttachedAgent, manifest_digest, manifest_of
from daimon.core.server_skills.sync import (
    NeedsReconfirm,
    SyncDeps,
    TargetError,
    UpstreamGone,
    approve_update,
    check_mirror,
    follow_now,
)
from daimon.core.skill_library_files import skill_lock
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
    unlisted_tools: bool = False
    """The listing leaves `allowed-tools` out, whatever SKILL.md says."""

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
        entry = self.current()
        if not self.unlisted_tools:
            return entry
        listed = {k: v for k, v in entry.frontmatter.items() if k != "allowed-tools"}
        return dataclasses.replace(entry, frontmatter=listed)

    async def fetch(self, mirror: ServerSkillRow) -> ServerSkill:
        return ServerSkill(entry=self.current(), files=dict(self.server.skills[NAME]))


@dataclass
class FakeTarget:
    provider: str
    agents: dict[str, str | None]
    failing: bool = False
    refuses: set[str] = field(default_factory=set[str])
    held: set[str] = field(default_factory=set[str])
    """Agents whose backend consent would not survive the update."""
    published: list[str] = field(default_factory=list[str])

    async def publish(self, mirror: ServerSkillRow, bundle: SkillBundle) -> str:
        if self.failing:
            raise TargetError("down")
        self.published.append(bundle.preview.name)
        return str(len(self.published) + 1)

    async def attached(self, mirror: ServerSkillRow) -> list[AttachedAgent]:
        return [AttachedAgent(agent, version) for agent, version in self.agents.items()]

    async def repin(self, mirror: ServerSkillRow, agent_id: str, version: str) -> None:
        if self.failing or agent_id in self.refuses:
            raise TargetError("down")
        if agent_id in self.held:
            raise NeedsReconfirm("drops differ")
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
        server="data",
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


async def test_one_refused_agent_does_not_hold_back_the_others(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world = await _world(db_session, db_session_factory)
    world.first.agents = {"archived": "1", "follows": "1"}
    world.first.refuses = {"archived"}
    world.upstream.server.skills[NAME]["guide.md"] = b"More.\n"

    assert await check_mirror(world.deps, world.mirror.id) == "failed"
    assert world.first.agents == {"archived": "1", "follows": "2"}
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

    assert await approve_update(world.deps, world.mirror.id, digest=row.pending_digest) == "updated"
    row = await _row(world)
    assert row.status == "current" and row.pending_digest is None
    assert world.first.agents["follows"] == "2"


async def test_tools_are_read_from_the_checked_skill_md(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world = await _world(db_session, db_session_factory)
    world.upstream.tools = "Bash"
    world.upstream.unlisted_tools = True
    world.upstream.server.skills[NAME]["SKILL.md"] = skill_md(NAME).replace(
        b"---\nDo", b"allowed-tools: Bash\n---\nDo"
    )
    assert await check_mirror(world.deps, world.mirror.id) == "pending"
    assert world.first.agents["follows"] == "1"

    digest = (await _row(world)).pending_digest
    assert await approve_update(world.deps, world.mirror.id, digest=str(digest)) == "updated"
    row = await _row(world)
    assert row.allowed_tools == "Bash", "the approved tools come from SKILL.md, not the listing"
    assert world.first.agents["follows"] == "2"


async def _held(world: World, agent_id: str) -> dict[str, str]:
    async with world.deps.sessionmaker() as session:
        return await store.held_updates(
            session, tenant_id=world.mirror.tenant_id, agent_id=agent_id
        )


async def test_an_agent_whose_consent_would_change_is_held_not_moved(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A held agent keeps its version and shows the update; the others move on."""
    world = await _world(db_session, db_session_factory)
    world.first.held = {"follows"}
    world.upstream.server.skills[NAME]["guide.md"] = b"More.\n"

    assert await check_mirror(world.deps, world.mirror.id) == "updated", "a hold is no failure"
    assert world.first.agents["follows"] == "1", "a held agent is never moved"
    assert world.second.agents == {"other": "2"}
    digest = (await _row(world)).digest
    assert await _held(world, "follows") == {"skill_1": digest}

    world.first.held = set()
    assert await check_mirror(world.deps, world.mirror.id) == "unchanged"
    assert world.first.agents["follows"] == "2", "the hold is retried each run"
    assert await _held(world, "follows") == {}, "a moved agent's hold is released"


async def test_a_hold_is_released_once_the_agent_is_current(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An owner who moved the agent by hand clears the stale hold on the next run."""
    world = await _world(db_session, db_session_factory)
    world.first.held = {"follows"}
    world.upstream.server.skills[NAME]["guide.md"] = b"More.\n"
    assert await check_mirror(world.deps, world.mirror.id) == "updated"

    world.first.agents["follows"] = "2"
    assert await check_mirror(world.deps, world.mirror.id) == "unchanged"
    assert await _held(world, "follows") == {}, "an agent already current holds nothing"


async def test_an_agent_that_resumes_following_moves_at_once(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Following again repins to the deployed version now, not on the next check."""
    world = await _world(db_session, db_session_factory)
    tenant = world.mirror.tenant_id
    async with world.deps.sessionmaker.begin() as session:
        await store.put_deployment(
            session,
            mirror_id=world.mirror.id,
            tenant_id=tenant,
            provider="first",
            digest=world.mirror.digest,
            version="5",
            now=NOW,
        )
    await follow_now(world.deps, tenant_id=tenant, skill_id="skill_1", agent_id="pins")
    assert world.first.agents["pins"] == "1", "a pinned agent stays until it follows"

    async with world.deps.sessionmaker.begin() as session:
        await store.set_follow(
            session, tenant_id=tenant, agent_id="pins", skill_id="skill_1", follow=True, now=NOW
        )
    world.first.held = {"follows"}
    await follow_now(world.deps, tenant_id=tenant, skill_id="skill_1", agent_id="pins")
    assert world.first.agents == {"follows": "1", "pins": "5"}, "only that agent moves"

    world.first.held = {"pins"}
    world.first.agents["pins"] = "1"
    await follow_now(world.deps, tenant_id=tenant, skill_id="skill_1", agent_id="pins")
    assert world.first.agents["pins"] == "1", "the consent rules still apply"
    assert await _held(world, "pins") == {"skill_1": world.mirror.digest}


async def test_following_again_survives_a_failing_provider(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The choice is saved before the move, so a failed move is left to the next sync."""
    world = await _world(db_session, db_session_factory)

    async def broken(_mirror: ServerSkillRow) -> list[AttachedAgent]:
        raise RuntimeError("database down")

    world.first.attached = broken  # pyright: ignore[reportAttributeAccessIssue]
    await follow_now(world.deps, tenant_id=world.mirror.tenant_id, skill_id="skill_1", agent_id="x")


async def test_a_listing_never_rewrites_the_accepted_tools(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world = await _world(db_session, db_session_factory)
    world.upstream.tools = "Bash"
    assert await check_mirror(world.deps, world.mirror.id) == "unchanged"
    assert (await _row(world)).allowed_tools is None


async def test_approval_applies_only_the_reviewed_update(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world = await _world(db_session, db_session_factory)
    world.upstream.server.skills[NAME]["run.py"] = b"print(1)\n"
    assert await check_mirror(world.deps, world.mirror.id) == "pending"
    reviewed = (await _row(world)).pending_digest
    assert reviewed is not None
    world.upstream.server.skills[NAME]["run.py"] = b"print(2)\n"
    assert await check_mirror(world.deps, world.mirror.id) == "pending"
    newer = (await _row(world)).pending_digest
    assert newer not in (None, reviewed)

    assert await approve_update(world.deps, world.mirror.id, digest=reviewed) == "pending"
    assert world.first.published == [] and world.first.agents["follows"] == "1"
    assert (await _row(world)).pending_digest == newer, "the newer update still waits"

    assert await approve_update(world.deps, world.mirror.id, digest=str(newer)) == "updated"
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


async def test_a_mirror_of_another_server_is_unavailable(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world = await _world(db_session, db_session_factory)
    world.upstream.server.skills[NAME]["guide.md"] = b"Other server.\n"
    deps = dataclasses.replace(world.deps, server="other")
    assert await check_mirror(deps, world.mirror.id) == "unavailable"
    assert world.first.published == [] and world.first.agents["follows"] == "1"


async def test_unknown_mirror_fails(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world = await _world(db_session, db_session_factory)
    assert await check_mirror(world.deps, uuid.uuid4()) == "failed"


@dataclass
class SlowTarget(FakeTarget):
    """Pauses while reading its agents, as a sync does between reading the pins and repinning."""

    reading: asyncio.Event = field(default_factory=asyncio.Event)
    resume: asyncio.Event = field(default_factory=asyncio.Event)

    async def attached(self, mirror: ServerSkillRow) -> list[AttachedAgent]:
        self.reading.set()
        await self.resume.wait()
        return await super().attached(mirror)


async def test_a_pin_chosen_mid_sync_waits_for_it_and_then_holds(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sync reads the pins once, so a choice that lands during it would be ignored."""
    world = await _world(db_session, db_session_factory)
    tenant = world.mirror.tenant_id
    edit: Any = SimpleNamespace(session_factory=db_session_factory)
    slow = SlowTarget("first", {"follows": "1", "pins": "1"})

    async def target(*_a: object, **_k: object) -> Any:
        return SimpleNamespace(
            agent=SimpleNamespace(id="follows", name="follows"), managed=False, sees_setup=True
        )

    monkeypatch.setattr(follows_module, "load_target", target)

    def lock(mirror: ServerSkillRow) -> AbstractAsyncContextManager[None]:
        return skill_lock(edit, tenant_id=mirror.tenant_id, skill_id=mirror.skill_id)

    deps = dataclasses.replace(world.deps, targets=(slow, world.second), lock=lock)
    world.upstream.server.skills[NAME]["guide.md"] = b"More.\n"
    sync = asyncio.create_task(check_mirror(deps, world.mirror.id))
    await slow.reading.wait()
    pin = asyncio.create_task(
        set_agent_follow(
            edit,
            actor=SimpleNamespace(tenant_id=tenant),  # pyright: ignore[reportArgumentType]
            agent_id="ag",
            skill_id="skill_1",
            follow=False,
            now=NOW,
        )
    )
    await asyncio.sleep(0.05)
    assert not pin.done(), "the choice waits for the sync that read the old pins"
    slow.resume.set()
    assert await sync == "updated"
    await pin

    world.upstream.server.skills[NAME]["guide.md"] = b"Again.\n"
    assert await check_mirror(deps, world.mirror.id) == "updated"
    assert slow.agents["follows"] == "2", "the next sync leaves the agent on the version it pinned"
    assert world.second.agents == {"other": "3"}
