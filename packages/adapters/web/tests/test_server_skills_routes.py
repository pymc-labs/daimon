"""`/api/agents/server-skills`: the node server's skills offered, added and followed."""

from __future__ import annotations

import re

import httpx
from anthropic.types.beta import SkillListResponse
from daimon.core.config import ServerSkillsSettings
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.mcp_identity import generate_signing_key
from daimon.testing.mcp_skills import SkillsServer, skill_md
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_nodes import _OWN, _client, _Server, _settings, _setup


class _Node(_Server):
    """The node's identity server, which also serves skills at its MCP endpoint."""

    def __init__(self, skills: SkillsServer) -> None:
        super().__init__()
        self.skills = skills

    async def handler(self, request: httpx.Request) -> httpx.Response:  # pyright: ignore[reportIncompatibleMethodOverride]
        if request.url.path == "/mcp":
            return await self.skills.handle(request)
        return super().handler(request)


def _created(_r: httpx.Request, _m: object) -> httpx.Response:
    body = SkillListResponse(
        id="sk_new",
        type="custom",
        display_title="x",
        latest_version="1",
        created_at="2030-01-01T00:00:00Z",
        updated_at="2030-01-01T00:00:00Z",
        source="custom",
    )
    return httpx.Response(200, json=body.model_dump(mode="json"))


async def test_without_the_setting_there_is_no_menu(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _ = await _setup(db_session)
    key = generate_signing_key()
    async with _client(agents, _Server(), db_session_factory, owner, key=key) as client:
        res = await client.get("/api/agents/server-skills")
    assert res.status_code == 404


async def test_a_skill_is_offered_added_and_pinned(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _ = await _setup(db_session)
    agents.router.add("POST", r"/v1/skills$", _created)
    key = generate_signing_key()
    settings = _settings(key).model_copy(
        update={"server_skills": ServerSkillsSettings(enabled=True)}
    )
    node = _Node(SkillsServer(skills={"plots": {"SKILL.md": skill_md("plots")}}))
    async with _client(
        agents, node, db_session_factory, owner, key=key, settings=settings
    ) as client:
        before = await client.get("/api/agents/server-skills")
        added = await client.post(
            "/api/agents/server-skills", json={"uri": node.skills.uri("plots")}, headers=_OWN
        )
        after = await client.get("/api/agents/server-skills")
        unknown = await client.put(
            "/api/agents/ag_w/skill-follows",
            json={"skill_id": "sk_other", "follow": False},
            headers=_OWN,
        )
        pinned = await client.put(
            "/api/agents/ag_w/skill-follows",
            json={"skill_id": "sk_new", "follow": False},
            headers=_OWN,
        )
        follows = await client.get("/api/agents/ag_w/skill-follows")
        check = await client.post("/api/agents/skills/sk_new/check", headers=_OWN)

    assert before.status_code == 200, before.text
    assert before.json() == {
        "label": "Data server",
        "skills": [
            {
                "uri": node.skills.uri("plots"),
                "name": "plots",
                "description": "Helps with plots.",
                "skill_id": None,
                "taken": False,
            }
        ],
    }
    assert added.status_code == 201, added.text
    assert added.json()["id"] == "sk_new"
    assert after.json()["skills"][0]["skill_id"] == "sk_new", "an added skill is marked"
    assert unknown.status_code == 404
    assert pinned.status_code == 204, pinned.text
    assert follows.json() == {"sk_new": {"follow": False, "status": "current", "version": "1"}}
    assert check.status_code == 403, "only an admin checks the library"


async def test_a_skill_whose_name_is_taken_is_marked(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _ = await _setup(db_session)
    title = tenant_scoped_display_title(tenant_id=owner.tenant_id, name="plots")
    held = _created(httpx.Request("GET", "https://api.example.com"), None).json()
    mine = {"data": [{**held, "id": "sk_mine", "display_title": title}], "has_more": False}
    listing = re.compile(r"/v1/skills$")
    agents.router.routes.insert(0, ("GET", listing, lambda _r, _m: httpx.Response(200, json=mine)))
    key = generate_signing_key()
    settings = _settings(key).model_copy(
        update={"server_skills": ServerSkillsSettings(enabled=True)}
    )
    node = _Node(SkillsServer(skills={"plots": {"SKILL.md": skill_md("plots")}}))
    async with _client(
        agents, node, db_session_factory, owner, key=key, settings=settings
    ) as client:
        res = await client.get("/api/agents/server-skills")
    assert res.json()["skills"][0]["taken"] is True
