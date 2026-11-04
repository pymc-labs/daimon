"""`/api/agents/server-skills`: the node server's skills offered, added and followed."""

from __future__ import annotations

import re

import httpx
from anthropic.types.beta import SkillListResponse
from daimon.adapters.web.server_skills import active_adder
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.mcp_identity import generate_signing_key
from daimon.core.stores import server_skills as server_skills_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores import workspace_members as members_store
from daimon.testing.factories import (
    make_account,
    make_platform_principal,
    make_tenant,
    make_web_person,
)
from daimon.testing.mcp_skills import SkillsServer, skill_md
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_nodes import _NOW, _OWN, _client, _Server, _settings, _setup


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


async def test_without_a_data_server_node_there_is_no_menu(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _ = await _setup(db_session)
    key = generate_signing_key()
    base = _settings(key)
    assert base.web is not None
    nodes = base.web.nodes.model_copy(update={"data_server": None})
    settings = base.model_copy(update={"web": base.web.model_copy(update={"nodes": nodes})})
    async with _client(
        agents, _Server(), db_session_factory, owner, key=key, settings=settings
    ) as client:
        res = await client.get("/api/agents/server-skills")
    assert res.status_code == 404


async def test_a_skill_is_offered_added_and_pinned(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _ = await _setup(db_session)
    agents.router.add("POST", r"/v1/skills$", _created)
    key = generate_signing_key()
    settings = _settings(key)
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
    settings = _settings(key)
    node = _Node(SkillsServer(skills={"plots": {"SKILL.md": skill_md("plots")}}))
    async with _client(
        agents, node, db_session_factory, owner, key=key, settings=settings
    ) as client:
        res = await client.get("/api/agents/server-skills")
    assert res.json()["skills"][0]["taken"] is True


async def test_the_update_check_asks_only_as_an_adder_who_could_sign_in(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g-1")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="discord", external_id="u-1", tenant=tenant, account=account
    )
    person = await make_web_person(db_session, tenant=tenant, account=account)
    mirror = await server_skills_store.create_mirror(
        db_session,
        tenant_id=tenant.id,
        server="data",
        uri="skill://plots",
        name="plots",
        skill_id="sk_1",
        version_label=None,
        digest="d",
        manifest={},
        allowed_tools=None,
        added_by_person_id=person.id,
        now=_NOW,
    )
    await db_session.commit()

    async def adder() -> object:
        found = await active_adder(db_session_factory, mirror)
        return None if found is None else found.id

    assert await adder() == person.id
    await members_store.replace_members(
        db_session, tenant_id=tenant.id, members={"u-2": False}, workspace_name="w", now=_NOW
    )
    await db_session.commit()
    assert await adder() is None, "they left the chat workspace"
    await members_store.replace_members(
        db_session, tenant_id=tenant.id, members={"u-1": False}, workspace_name="w", now=_NOW
    )
    await people_store.update_person(db_session, person_id=person.id, status="suspended")
    await db_session.commit()
    assert await adder() is None, "they are suspended"


async def test_a_held_update_is_shown_and_applied_by_the_owner(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An update the backend consent held back waits for the owner's own move."""
    agents, owner, _ = await _setup(db_session)
    agents.router.add("POST", r"/v1/skills$", _created)
    key = generate_signing_key()
    settings = _settings(key)
    node = _Node(SkillsServer(skills={"plots": {"SKILL.md": skill_md("plots")}}))
    async with _client(
        agents, node, db_session_factory, owner, key=key, settings=settings
    ) as client:
        await client.post(
            "/api/agents/server-skills", json={"uri": node.skills.uri("plots")}, headers=_OWN
        )
        nothing = await client.post("/api/agents/ag_w/skill-follows/sk_new/apply", headers=_OWN)
        async with db_session_factory.begin() as session:
            mirror = await server_skills_store.mirror_of_skill(
                session, tenant_id=owner.tenant_id, skill_id="sk_new"
            )
            assert mirror is not None
            await server_skills_store.hold_update(
                session,
                tenant_id=owner.tenant_id,
                agent_id="ag_w",
                skill_id="sk_new",
                digest=mirror.digest,
                reason="drops differ",
                now=_NOW,
            )
        held = await client.get("/api/agents/ag_w/skill-follows")
        applied = await client.post("/api/agents/ag_w/skill-follows/sk_new/apply", headers=_OWN)
        after = await client.get("/api/agents/ag_w/skill-follows")

    assert nothing.status_code == 409, "nothing waits before a hold"
    assert held.json()["sk_new"]["status"] == "needs_reconfirm", held.text
    assert applied.status_code == 204, applied.text
    assert after.json()["sk_new"]["status"] == "current", "the hold is released"
