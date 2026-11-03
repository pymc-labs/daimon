"""`/api/agents/{id}/archive` and `/copy`: only an admin retires or copies an agent."""

from __future__ import annotations

import re
from typing import Any

import httpx
from daimon.core.agent_ownership import owns_agent
from daimon.core.stores import web_audit
from mux.testing.drivers.anthropic.ma import json_body
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import OWN, Agents, agent_client, agent_people, make_admin, sign_in_token


def _archiving(agents: Agents) -> list[str]:
    archived: list[str] = []

    def archive(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        archived.append("ag_w")
        return httpx.Response(
            200,
            json={**agents.agent.model_dump(mode="json"), "archived_at": "2030-01-15T12:00:00Z"},
        )

    agents.router.add("POST", r"/v1/agents/ag_w/archive$", archive)
    return archived


def _copying(agents: Agents) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []

    def create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = json_body(req)
        created.append(body)
        made = {**agents.agent.model_dump(mode="json"), **body, "id": "ag_copy", "version": 1}
        return httpx.Response(200, json=made)

    agents.router.add("POST", r"/v1/agents$", create)
    agents.router.add(
        "GET", r"/v1/agents/ag_copy$", lambda _r, _m: httpx.Response(200, json=created[-1])
    )
    return created


async def test_an_admin_archives_an_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    archived = _archiving(agents)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        res = await client.post("/api/agents/ag_w/archive", headers=OWN)
    assert res.status_code == 204, res.text
    assert archived == ["ag_w"], "the agent is archived in Managed Agents"
    async with db_session_factory() as session:
        rows = await web_audit.list_recent(session, tenant_id=owner.tenant_id)
    assert [(r.action, r.target) for r in rows] == [("Archived agent", "helper")]


async def test_an_owner_who_is_not_an_admin_cannot_archive_or_copy(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await agent_people(db_session)
    archived = _archiving(agents)
    created = _copying(agents)
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        gone = await client.post("/api/agents/ag_w/archive", headers=OWN)
        copy = await client.post("/api/agents/ag_w/copy", json={"name": "twin"}, headers=OWN)
    assert (gone.status_code, copy.status_code) == (403, 403)
    assert archived == [] and created == [], "nothing was changed"


async def test_a_managed_agent_is_not_archived(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    agents.agent = agents.agent.model_copy(
        update={"metadata": {**agents.agent.metadata, "daimon_managed": "true"}}
    )
    await make_admin(db_session_factory, other)
    archived = _archiving(agents)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        res = await client.post("/api/agents/ag_w/archive", headers=OWN)
    assert res.status_code == 403 and archived == []


async def test_an_admin_copies_an_agent_and_owns_the_copy(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    created = _copying(agents)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        res = await client.post("/api/agents/ag_w/copy", json={"name": "twin"}, headers=OWN)
    assert res.status_code == 200, res.text
    assert res.json() == {"id": "ag_copy", "name": "twin"}
    assert [c["name"] for c in created] == ["twin"] and created[0]["system"].endswith("Be brief.")
    async with db_session_factory() as session:
        rows = await web_audit.list_recent(session, tenant_id=owner.tenant_id)
        owned = await owns_agent(
            session, tenant_id=owner.tenant_id, ma_agent_id="ag_copy", account_id=other.account_id
        )
    assert [(r.action, r.target) for r in rows] == [("Copied agent", "twin")]
    assert owned, "the admin who made the copy owns it"
