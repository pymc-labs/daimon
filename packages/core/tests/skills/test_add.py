"""add_agent_skill against a fake MA transport and a real ledger."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent, SkillListResponse
from anthropic.types.beta.skills import VersionCreateResponse
from daimon.core.constants import AGENT_SKILL_CAP
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.skills.add import add_agent_skill
from daimon.core.skills.ingest import SkillIngestError, bundle_from_markdown
from daimon.core.stores.user_skills import load_user_skill, upsert_user_skill
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TS = "2026-04-21T00:00:00Z"
_TOOLSET = {
    "type": "agent_toolset_20260401",
    "configs": [],
    "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
}


def _md(body: str = "Write them down.") -> str:
    return f"---\nname: notes\ndescription: Take meeting notes.\n---\n{body}\n"


def _skill(skill_id: str, title: str) -> dict[str, Any]:
    return SkillListResponse(
        id=skill_id,
        type="custom",
        display_title=title,
        latest_version="1",
        created_at=_TS,
        updated_at=_TS,
        source="custom",
    ).model_dump(mode="json")


@dataclass
class _FakeMA:
    agent: BetaManagedAgentsAgent
    skills: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    created_titles: list[str] = field(default_factory=list[str])
    versions: list[str] = field(default_factory=list[str])
    updates: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def router(self) -> MARouter:
        router = MARouter()
        router.add("GET", r"/v1/skills", lambda _r, _m: list_response(self.skills))
        router.add("POST", r"/v1/skills", self._create)
        router.add("POST", r"/v1/skills/(?P<id>[^/]+)/versions", self._version)
        router.add("GET", rf"/v1/agents/{self.agent.id}", self._agent)
        router.add("POST", rf"/v1/agents/{self.agent.id}", self._update)
        return router

    def _create(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        found = re.search(rb'name="display_title"\r\n\r\n([^\r]+)', request.content)
        assert found is not None, "skills.create must send a display_title"
        title = found.group(1).decode()
        self.created_titles.append(title)
        created = _skill(f"sk_{len(self.skills)}", title)
        self.skills.append(created)
        return httpx.Response(200, json=created)

    def _version(self, _r: httpx.Request, match: re.Match[str]) -> httpx.Response:
        self.versions.append(match["id"])
        return httpx.Response(
            200,
            json=VersionCreateResponse(
                id="ver_2",
                skill_id=match["id"],
                version="2",
                type="skill_version",
                name="SKILL.zip",
                directory="/",
                description="",
                created_at=_TS,
            ).model_dump(mode="json"),
        )

    def _agent(self, _r: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(200, json=self.agent.model_dump(mode="json"))

    def _update(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = json.loads(request.content)
        self.updates.append(body)
        return httpx.Response(200, json=self.agent.model_dump(mode="json"))


async def _add(
    fake: _FakeMA,
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    text: str | None = None,
    added_by: uuid.UUID | None = None,
):
    return await add_agent_skill(
        build_fake_anthropic(fake.router().dispatch),
        factory,
        tenant_id=tenant_id,
        agent=fake.agent,
        agent_name="agent",
        bundle=bundle_from_markdown(text or _md()),
        origin="pasted",
        added_by_account_id=added_by,
    )


async def test_new_skill_is_uploaded_agent_scoped_recorded_and_attached(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    fake = _FakeMA(agent=ma_agent(id="ag_1", name="agent", tenant_id=tenant.id))

    result = await _add(fake, db_session_factory, tenant.id, added_by=account.id)

    assert (result.action, result.newly_attached) == ("created", True)
    assert fake.created_titles == [
        tenant_scoped_display_title(tenant_id=tenant.id, name="notes", agent_name="agent")
    ], "an added skill is always agent-scoped, never the shared library"
    (update,) = fake.updates
    assert update["skills"] == [{"type": "custom", "skill_id": result.skill_id}]
    assert _TOOLSET["type"] in {tool["type"] for tool in update["tools"]}, (
        "a skill needs the base toolset, so attaching adds it"
    )
    row = await load_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_1"),
        agent_name="agent",
        name="notes",
    )
    assert row is not None
    assert (row.source, row.origin, row.added_by_account_id) == ("upload", "pasted", account.id)
    assert row.source_repo_url == "", "no repo owns it, so no repo sync deletes it"


async def test_re_adding_pushes_a_version_only_when_the_content_changed(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    fake = _FakeMA(agent=ma_agent(id="ag_1", name="agent", tenant_id=tenant.id, tools=[_TOOLSET]))
    first = await _add(fake, db_session_factory, tenant.id)
    fake.agent = ma_agent(
        id="ag_1",
        name="agent",
        tenant_id=tenant.id,
        tools=[_TOOLSET],
        skills=[{"type": "custom", "skill_id": first.skill_id, "version": "1"}],
    )

    same = await _add(fake, db_session_factory, tenant.id)
    changed = await _add(fake, db_session_factory, tenant.id, text=_md("Write them twice."))

    assert (same.action, same.newly_attached) == ("unchanged", False)
    assert (changed.action, changed.skill_id) == ("updated", first.skill_id)
    assert fake.versions == [first.skill_id], "only the changed content is pushed"
    assert len(fake.created_titles) == 1, "re-adding never duplicates the skill"
    assert "tools" not in fake.updates[0], "an agent with the base toolset keeps its tools"


async def test_a_skill_from_the_agents_skill_repo_is_not_replaced(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    fake = _FakeMA(agent=ma_agent(id="ag_1", name="agent", tenant_id=tenant.id))
    await upsert_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_1"),
        agent_name="agent",
        name="notes",
        source_repo_url="https://github.com/o/r",
        source_repo_branch="main",
        source_path="notes",
        content_hash="h",
        anthropic_id="sk_repo",
        anthropic_latest_version="1",
    )

    with pytest.raises(SkillIngestError, match="comes from the skill repo"):
        await _add(fake, db_session_factory, tenant.id)
    assert fake.created_titles == [] and fake.updates == []


async def test_a_shared_skill_of_the_same_name_is_never_shadowed(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    shared = tenant_scoped_display_title(tenant_id=tenant.id, name="notes", agent_name=None)
    fake = _FakeMA(
        agent=ma_agent(id="ag_1", name="agent", tenant_id=tenant.id),
        skills=[_skill("sk_shared", shared)],
    )

    with pytest.raises(SkillIngestError, match="shared or built-in"):
        await _add(fake, db_session_factory, tenant.id)
    assert fake.created_titles == [] and fake.versions == [], "the shared skill is untouched"


async def test_an_agent_at_the_skill_cap_is_refused_before_uploading(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    full = [
        {"type": "custom", "skill_id": f"sk_{i}", "version": "1"} for i in range(AGENT_SKILL_CAP)
    ]
    fake = _FakeMA(agent=ma_agent(id="ag_1", name="agent", tenant_id=tenant.id, skills=full))

    with pytest.raises(SkillIngestError, match="Remove one first"):
        await _add(fake, db_session_factory, tenant.id)
    assert fake.created_titles == []
