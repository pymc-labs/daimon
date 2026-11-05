"""`PUT /api/agents/{id}/helpers`: an admin sets a seeded agent's helpers for their own
workspace; nobody else does, and no other workspace's list moves."""

from __future__ import annotations

import re
import uuid

import httpx
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.stores import agent_helpers, web_audit
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import OWN, Agents, agent_client, agent_people, make_admin, sign_in_token

SYSTEM = "ag_sys"


class WithSystemAgent(Agents):
    """The workspace's agent plus its own copy of the seeded default agent."""

    def __init__(self, agents: Agents, tenant_id: uuid.UUID) -> None:
        super().__init__(agents.agent)
        self.system = ma_agent(
            id=SYSTEM,
            name="daimon",
            tenant_id=tenant_id,
            metadata={MA_METADATA_KEY_MANAGED: "true"},
            system="Be brief.",
        )
        self.router.add("GET", rf"/v1/agents/{SYSTEM}$", lambda _r, _m: self._system_json())

    def _system_json(self) -> httpx.Response:
        return httpx.Response(200, json=self.system.model_dump(mode="json"))

    def _list(self) -> httpx.Response:
        data = [a.model_dump(mode="json") for a in (self.agent, self.system)]
        return httpx.Response(200, json={"data": data, "has_more": False})


def _helpers(body: dict[str, object]) -> list[str]:
    return [h["id"] for h in body["helpers"]]  # type: ignore[index, union-attr]


async def test_an_admin_sets_a_seeded_agents_helpers(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    base, owner, other = await agent_people(db_session)
    agents = WithSystemAgent(base, owner.tenant_id)
    await make_admin(db_session_factory, other)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        before = (await client.get(f"/api/agents/{SYSTEM}")).json()
        put = await client.put(
            f"/api/agents/{SYSTEM}/helpers", json={"helpers": ["ag_w"]}, headers=OWN
        )
        after = (await client.get(f"/api/agents/{SYSTEM}")).json()
    assert before["can_edit"] is False and before["can_edit_helpers"] is True, (
        "an admin edits only the helper list of a seeded agent"
    )
    assert put.status_code == 200, put.text
    assert _helpers(put.json()) == ["ag_w"], "the response is the agent as now saved"
    assert _helpers(after) == ["ag_w"]
    assert agents.updates == [], "the provider spec is never written"
    async with db_session_factory() as session:
        rows = await web_audit.list_recent(session, tenant_id=owner.tenant_id)
    assert [r.action for r in rows] == ["Changed agent helpers"]


async def test_a_member_who_is_not_an_admin_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    base, owner, _other = await agent_people(db_session)
    agents = WithSystemAgent(base, owner.tenant_id)
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        view = (await client.get(f"/api/agents/{SYSTEM}")).json()
        put = await client.put(
            f"/api/agents/{SYSTEM}/helpers", json={"helpers": ["ag_w"]}, headers=OWN
        )
    assert view["can_edit_helpers"] is False
    assert put.status_code == 403, put.text
    async with db_session_factory() as session:
        stored = await agent_helpers.list_helper_ids(
            session, tenant_id=owner.tenant_id, agent_id=SYSTEM
        )
    assert stored == [], "nothing was written"


async def test_an_ordinary_agent_keeps_the_draft_flow(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    base, owner, other = await agent_people(db_session)
    agents = WithSystemAgent(base, owner.tenant_id)
    await make_admin(db_session_factory, other)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        view = (await client.get("/api/agents/ag_w")).json()
        put = await client.put("/api/agents/ag_w/helpers", json={"helpers": [SYSTEM]}, headers=OWN)
    assert view["can_edit_helpers"] is False
    assert put.status_code == 422, put.text


async def test_an_agent_cannot_be_its_own_helper_or_use_a_missing_one(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    base, owner, other = await agent_people(db_session)
    agents = WithSystemAgent(base, owner.tenant_id)
    await make_admin(db_session_factory, other)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        itself = await client.put(
            f"/api/agents/{SYSTEM}/helpers", json={"helpers": [SYSTEM]}, headers=OWN
        )
        missing = await client.put(
            f"/api/agents/{SYSTEM}/helpers", json={"helpers": ["ag_nope"]}, headers=OWN
        )
        unknown = await client.put(
            "/api/agents/ag_nope/helpers", json={"helpers": ["ag_w"]}, headers=OWN
        )
        after = (await client.get(f"/api/agents/{SYSTEM}")).json()
    assert (itself.status_code, missing.status_code) == (422, 422)
    assert unknown.status_code == 404
    assert _helpers(after) == []


async def test_one_workspaces_edit_leaves_the_other_workspaces_list_empty(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    base_a, owner_a, admin_a = await agent_people(db_session)
    base_b, owner_b, admin_b = await agent_people(db_session)
    agents_a = WithSystemAgent(base_a, owner_a.tenant_id)
    agents_b = WithSystemAgent(base_b, owner_b.tenant_id)
    await make_admin(db_session_factory, admin_a)
    await make_admin(db_session_factory, admin_b)
    token_a = await sign_in_token(db_session_factory, admin_a)
    token_b = await sign_in_token(db_session_factory, admin_b)
    async with (
        agent_client(agents_a, owner_a.tenant_id, db_session_factory, token_a) as a,
        agent_client(agents_b, owner_b.tenant_id, db_session_factory, token_b) as b,
    ):
        put = await a.put(f"/api/agents/{SYSTEM}/helpers", json={"helpers": ["ag_w"]}, headers=OWN)
        in_a = (await a.get(f"/api/agents/{SYSTEM}")).json()
        in_b = (await b.get(f"/api/agents/{SYSTEM}")).json()
    assert put.status_code == 200, put.text
    assert _helpers(in_a) == ["ag_w"]
    assert _helpers(in_b) == [], "the same agent id in the other workspace is untouched"
    assert re.fullmatch(r"[0-9a-f-]{36}", str(owner_a.tenant_id)) and owner_a.tenant_id != (
        owner_b.tenant_id
    )
    async with db_session_factory() as session:
        rows_b = await web_audit.list_recent(session, tenant_id=owner_b.tenant_id)
    assert rows_b == [], "nothing is recorded in the other workspace"
