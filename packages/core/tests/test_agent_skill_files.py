"""Saving a skill's files from the web editor: kept files come from the version the editor loaded."""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent, SkillListResponse
from anthropic.types.beta.skills import VersionCreateResponse, VersionListResponse
from daimon.core import agent_skill_files
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.authz import Subject, Surface
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import agent_drafts, agent_owners
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

_TS = "2026-04-21T00:00:00Z"
_SKILL_MD = "---\nname: notes\ndescription: Take notes.\n---\nWrite them down.\n"


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, body in files.items():
            archive.writestr(f"notes/{path}", body)
    return buffer.getvalue()


def _uploaded_files(request: httpx.Request) -> dict[str, bytes]:
    """The skill files inside a multipart upload (the archive is the only binary part)."""
    start = request.content.index(b"PK\x03\x04")
    with zipfile.ZipFile(io.BytesIO(request.content[start:])) as archive:
        return {
            name.removeprefix("notes/"): archive.read(name)
            for name in archive.namelist()
            if not name.endswith("/")
        }


@dataclass
class _FakeSkills:
    """One agent holding one skill whose versions are kept in memory."""

    agent: BetaManagedAgentsAgent
    title: str
    versions: dict[str, dict[str, bytes]] = field(default_factory=dict[str, dict[str, bytes]])
    downloads: list[str] = field(default_factory=list[str])

    def router(self) -> MARouter:
        router = MARouter()
        router.add("GET", r"/v1/skills$", lambda _r, _m: list_response([self._listed()]))
        router.add("GET", rf"/v1/agents/{self.agent.id}$", self._agent)
        router.add("GET", r"/v1/agents$", lambda _r, _m: list_response([self._agent_json()]))
        router.add("GET", r"/v1/skills/sk_1/versions$", self._listed_versions)
        router.add("POST", r"/v1/skills/sk_1/versions$", self._publish)
        router.add("GET", r"/v1/skills/sk_1/versions/(?P<version>[^/]+)/content$", self._content)
        return router

    def _listed(self) -> dict[str, Any]:
        return SkillListResponse(
            id="sk_1",
            type="custom",
            display_title=self.title,
            latest_version=max(self.versions),
            created_at=_TS,
            updated_at=_TS,
            source="custom",
        ).model_dump(mode="json")

    def _listed_versions(self, _r: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        rows = [
            VersionListResponse(
                id=f"ver_{version}",
                skill_id="sk_1",
                version=version,
                type="skill_version",
                name="SKILL.zip",
                directory="/",
                description="",
                created_at=_TS,
            ).model_dump(mode="json")
            for version in self.versions
        ]
        return httpx.Response(200, json={"data": rows, "has_more": False, "next_page": None})

    def _agent_json(self) -> dict[str, Any]:
        return self.agent.model_dump(mode="json")

    def _agent(self, _r: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(200, json=self._agent_json())

    def _publish(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        version = str(len(self.versions) + 1)
        self.versions[version] = _uploaded_files(request)
        created = VersionCreateResponse(
            id=f"ver_{version}",
            skill_id="sk_1",
            version=version,
            type="skill_version",
            name="SKILL.zip",
            directory="/",
            description="",
            created_at=_TS,
        )
        return httpx.Response(200, json=created.model_dump(mode="json"))

    def _content(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        # As the real endpoint: a workspace key sent with the skills beta header is
        # refused, and without it a version is named by its id.
        if "skills-2025-10-02" in request.headers.get("anthropic-beta", ""):
            return httpx.Response(
                403, json={"type": "error", "error": {"type": "permission_error"}}
            )
        version = match["version"].removeprefix("ver_")
        self.downloads.append(version)
        if version not in self.versions:
            return httpx.Response(404, json={"type": "error", "error": {"type": "not_found_error"}})
        return httpx.Response(200, content=_zip(self.versions[version]))


async def _setup(
    db_session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    *,
    live_pin: str,
) -> tuple[_FakeSkills, AgentEditDeps, AgentActor]:
    """A skill with two versions: v2 added ref.md and dropped nothing."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await agent_owners.record_creator(
        db_session,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_1"),
        account_id=account.id,
    )
    await db_session.commit()
    agent = ma_agent(
        id="ag_1",
        name="agent",
        tenant_id=tenant.id,
        metadata={"daimon_account": str(account.id)},
        skills=[{"type": "custom", "skill_id": "sk_1", "version": live_pin}],
    )
    fake = _FakeSkills(
        agent=agent,
        title=tenant_scoped_display_title(tenant_id=tenant.id, name="notes", agent_name="agent"),
        versions={
            "1": {"SKILL.md": _SKILL_MD.encode()},
            "2": {"SKILL.md": _SKILL_MD.encode(), "ref.md": b"from the first save"},
        },
    )
    deps = AgentEditDeps(
        client=build_fake_anthropic(fake.router().dispatch),
        session_factory=factory,
        public_url=None,
    )
    actor = AgentActor(
        tenant_id=tenant.id,
        account_id=account.id,
        subject=Subject(is_admin=False, platform_user_id=str(account.id)),
        surface=Surface.WEB,
    )
    return fake, deps, actor


async def test_a_kept_file_comes_from_the_base_version_not_the_live_pin(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The agent still pins v1 while the editor works on v2; the second save must not lose ref.md."""
    fake, deps, actor = await _setup(db_session, db_session_factory, live_pin="1")

    saved = await agent_skill_files.save_skill_files(
        deps,
        actor=actor,
        ma_agent_id="ag_1",
        skill_id="sk_1",
        files={"SKILL.md": _SKILL_MD + "More.\n", "ref.md": None},
        version="2",
    )

    assert saved.version == "3", "a new version is published"
    assert fake.versions["3"]["ref.md"] == b"from the first save", (
        "ref.md is read from v2, the version the editor loaded, not the live v1"
    )
    assert fake.downloads == ["2"], "the base version is downloaded once"


async def test_a_kept_file_defaults_to_the_version_the_draft_pins(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A client that names no base version still reads what its own draft pinned."""
    fake, deps, actor = await _setup(db_session, db_session_factory, live_pin="1")
    config: dict[str, Any] = {
        "name": "agent",
        "purpose": "",
        "avatar": "",
        "model": {
            "id": "claude-sonnet-4-5",
            "backend": "anthropic",
            "speed": "standard",
            "effort": "medium",
        },
        "system": None,
        "tools": [],
        "mcp_servers": [],
        "places": None,
        "skills": [
            {
                "type": "custom",
                "skill_id": "sk_1",
                "title": "notes",
                "description": "Take notes.",
                "version": "2",
            }
        ],
    }
    async with db_session_factory.begin() as session:
        await agent_drafts.save_draft(
            session,
            tenant_id=actor.tenant_id,
            agent_id="ag_1",
            account_id=actor.account_id,
            base_version=1,
            base_config={},
            config=config,
        )

    await agent_skill_files.save_skill_files(
        deps,
        actor=actor,
        ma_agent_id="ag_1",
        skill_id="sk_1",
        files={"SKILL.md": _SKILL_MD + "More.\n", "ref.md": None},
    )

    assert fake.versions["3"]["ref.md"] == b"from the first save", "the draft's pin wins over v1"


async def test_a_base_version_the_skill_does_not_have_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A version belongs to its skill: one that skill never published reads as not found."""
    fake, deps, actor = await _setup(db_session, db_session_factory, live_pin="1")

    with capture_logs() as logs, pytest.raises(AgentEditError) as refused:
        await agent_skill_files.save_skill_files(
            deps,
            actor=actor,
            ma_agent_id="ag_1",
            skill_id="sk_1",
            files={"SKILL.md": _SKILL_MD, "ref.md": None},
            version="99",
        )

    assert refused.value.kind == "not_found", "an unknown version is not found"
    failed = [e for e in logs if e["event"] == "agent_skill_files.read_failed"]
    assert [(e["step"], e["error"]) for e in failed] == [("download", "ProviderError")], (
        "the cause behind the refusal is logged"
    )
    assert sorted(fake.versions) == ["1", "2"], "nothing was published"


async def test_a_kept_file_the_base_version_lacks_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """ref.md exists in v2 only; keeping it against v1 is a stale editor, not a silent drop."""
    fake, deps, actor = await _setup(db_session, db_session_factory, live_pin="2")

    with pytest.raises(AgentEditError) as refused:
        await agent_skill_files.save_skill_files(
            deps,
            actor=actor,
            ma_agent_id="ag_1",
            skill_id="sk_1",
            files={"SKILL.md": _SKILL_MD, "ref.md": None},
            version="1",
        )

    assert refused.value.kind == "invalid", "the kept file is not in the base version"
    assert sorted(fake.versions) == ["1", "2"], "nothing was published"


async def test_an_oversize_file_is_refused_before_anything_is_downloaded(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The editor opens files up to MAX_EDIT_BYTES, so a larger body is not one of its saves."""
    fake, deps, actor = await _setup(db_session, db_session_factory, live_pin="2")
    big = "x" * (agent_skill_files.MAX_EDIT_BYTES + 1)

    with pytest.raises(AgentEditError) as refused:
        await agent_skill_files.save_skill_files(
            deps,
            actor=actor,
            ma_agent_id="ag_1",
            skill_id="sk_1",
            files={"SKILL.md": _SKILL_MD, "ref.md": big},
        )

    assert refused.value.kind == "invalid", "an oversize body is invalid"
    assert fake.downloads == [] and sorted(fake.versions) == ["1", "2"], "nothing ran"


async def test_files_of_a_skill_pinned_at_latest_come_from_its_newest_version(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Agents pin skills at "latest"; the editor reads, and saves against, the newest version."""
    fake, deps, actor = await _setup(db_session, db_session_factory, live_pin="latest")
    fake.versions["10"] = {"SKILL.md": _SKILL_MD.encode(), "new.md": b"newest"}

    doc = await agent_skill_files.read_skill_files(
        deps, actor=actor, ma_agent_id="ag_1", skill_id="sk_1"
    )

    assert doc.version == "10", "the newest version wins by number, not as text ('2' > '10')"
    assert [f.path for f in doc.files] == ["SKILL.md", "new.md"], "the newest files are listed"
    assert fake.downloads == ["10"], "latest itself is never downloaded"

    saved = await agent_skill_files.save_skill_files(
        deps,
        actor=actor,
        ma_agent_id="ag_1",
        skill_id="sk_1",
        files={"SKILL.md": _SKILL_MD + "More.\n", "new.md": None},
    )

    assert saved.version == "4", "a save publishes a new version"
    assert fake.versions["4"]["new.md"] == b"newest", "kept files come from the newest version"
