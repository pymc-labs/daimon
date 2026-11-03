"""`/api/agents/{id}/owners`: only an admin changes who owns an agent."""

from __future__ import annotations

import uuid

from daimon.core._models import WebPerson
from daimon.core.stores import web_audit
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import OWN, agent_client, agent_people, make_admin, sign_in_token


async def test_an_admin_changes_the_owner_and_the_co_owners(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        options = await client.get("/api/agents/ag_w/owners/options")
        put = await client.put(
            "/api/agents/ag_w/owners",
            json={"owner": str(other.id), "co_owners": [str(owner.id)]},
            headers=OWN,
        )
        agent = (await client.get("/api/agents/ag_w")).json()
    assert options.status_code == 200, options.text
    assert sorted(p["name"] for p in options.json()) == ["Other", "Test Person"], (
        "the choices are the workspace's active members"
    )
    assert put.status_code == 204, put.text
    assert agent["owner"]["name"] == "Other", "the new owner is shown"
    assert [p["name"] for p in agent["co_owners"]] == ["Test Person"], "the old owner co-owns"
    async with db_session_factory() as session:
        rows = await web_audit.list_recent(session, tenant_id=owner.tenant_id)
    assert [r.action for r in rows] == ["Changed agent owners"], "the change is audited once"


async def test_an_unchanged_set_writes_no_audit_row(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        put = await client.put(
            "/api/agents/ag_w/owners", json={"owner": str(owner.id), "co_owners": []}, headers=OWN
        )
    async with db_session_factory() as session:
        rows = await web_audit.list_recent(session, tenant_id=owner.tenant_id)
    assert put.status_code == 204 and rows == [], "saving what is already so records nothing"


async def test_an_owner_who_is_not_an_admin_cannot_change_owners(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        put = await client.put(
            "/api/agents/ag_w/owners", json={"owner": str(other.id), "co_owners": []}, headers=OWN
        )
        options = await client.get("/api/agents/ag_w/owners/options")
    assert (put.status_code, options.status_code) == (403, 403), "owners are an admin's to change"


async def test_a_person_outside_the_workspace_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        stranger = await client.put(
            "/api/agents/ag_w/owners",
            json={"owner": str(uuid.uuid4()), "co_owners": []},
            headers=OWN,
        )
        garbled = await client.put(
            "/api/agents/ag_w/owners", json={"owner": "nobody", "co_owners": []}, headers=OWN
        )
    assert (stranger.status_code, garbled.status_code) == (422, 422), "only workspace people own"


async def test_clearing_the_owner_leaves_the_agent_to_admins(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        put = await client.put(
            "/api/agents/ag_w/owners", json={"owner": None, "co_owners": []}, headers=OWN
        )
    owner_token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, owner_token) as client:
        keys = await client.get("/api/agents/ag_w/keys")
    assert put.status_code == 204, put.text
    assert keys.status_code == 403, "the former owner no longer sees the agent's setup"


async def test_an_inactive_member_cannot_be_made_the_owner(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        handed = await client.put(
            "/api/agents/ag_w/owners", json={"owner": str(other.id), "co_owners": []}, headers=OWN
        )
    async with db_session_factory() as db, db.begin():
        await db.execute(
            update(WebPerson).where(WebPerson.id == owner.id).values(status="suspended")
        )
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        put = await client.put(
            "/api/agents/ag_w/owners",
            json={"owner": str(owner.id), "co_owners": []},
            headers=OWN,
        )
    assert handed.status_code == 204, handed.text
    assert put.status_code == 422, "only an active member, as the options list"


async def test_a_co_owner_only_save_keeps_a_suspended_recorded_owner(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    async with db_session_factory() as db, db.begin():
        await db.execute(
            update(WebPerson).where(WebPerson.id == owner.id).values(status="suspended")
        )
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        put = await client.put(
            "/api/agents/ag_w/owners",
            json={"owner": str(owner.id), "co_owners": [str(other.id)]},
            headers=OWN,
        )
        agent = (await client.get("/api/agents/ag_w")).json()
    assert put.status_code == 204, put.text
    assert [p["name"] for p in agent["co_owners"]] == ["Other"], "the co-owner is saved"
