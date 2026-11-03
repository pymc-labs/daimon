"""`/api/agents/{id}/keys` and `/coding-keys`: names only, write-only values, one-time tokens."""

from __future__ import annotations

import uuid

from daimon.core.config import Settings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import web_audit
from daimon.core.stores.agent_files import list_agent_files
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import OWN, agent_client, agent_people, make_admin, sign_in_token

_VALUE = "s3cret-value-123"
_MCP = {
    "public_url": "https://mcp.example.com/mcp",
    "jwt_secret": "x" * 40,
}


def _settings() -> Settings:
    db = {"url": "postgresql+asyncpg://u:p@h/db"}
    return Settings(_env_file=None, database=db, mcp=_MCP)  # pyright: ignore[reportCallIssue, reportArgumentType]


async def test_an_owner_adds_replaces_and_removes_a_key_without_the_value_coming_back(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await agent_people(db_session)
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        added = await client.put(
            "/api/agents/ag_w/keys/SERVICE_API_KEY", json={"value": _VALUE}, headers=OWN
        )
        replaced = await client.put(
            "/api/agents/ag_w/keys/SERVICE_API_KEY", json={"value": _VALUE + "2"}, headers=OWN
        )
        listed = await client.get("/api/agents/ag_w/keys")
        removed = await client.delete("/api/agents/ag_w/keys/SERVICE_API_KEY", headers=OWN)
        gone = await client.get("/api/agents/ag_w/keys")
    assert added.status_code == 200 and replaced.status_code == 200, added.text + replaced.text
    assert [k["key"] for k in listed.json()] == ["SERVICE_API_KEY"], "the name is listed"
    for body in (added.text, replaced.text, listed.text):
        assert _VALUE not in body, "a value never comes back"
    assert removed.json() == {"removed": True}, "the removal says the key was there"
    assert gone.json() == [], "nothing is left"
    async with db_session_factory() as session:
        actions = [
            r.action for r in await web_audit.list_recent(session, tenant_id=owner.tenant_id)
        ]
        stored = await list_agent_files(
            session,
            tenant_id=owner.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=owner.tenant_id, ma_agent_id="ag_w"),
        )
    assert actions.count("Set agent key") == 2 and "Removed agent key" in actions, (
        "each change is in the audit trail"
    )
    assert stored == [], "the key is deleted from the store"


async def test_a_value_never_reaches_the_audit_trail(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await agent_people(db_session)
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        await client.put(
            "/api/agents/ag_w/keys/SERVICE_API_KEY", json={"value": _VALUE}, headers=OWN
        )
    async with db_session_factory() as session:
        rows = await web_audit.list_recent(session, tenant_id=owner.tenant_id)
    assert all(_VALUE not in f"{r.detail} {r.target}" for r in rows), "no value in any row"
    assert rows[0].target == "helper" and rows[0].detail == "SERVICE_API_KEY", (
        "the row names the agent and the key"
    )


async def test_a_member_who_does_not_own_the_agent_has_no_keys(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        listed = await client.get("/api/agents/ag_w/keys")
        put = await client.put(
            "/api/agents/ag_w/keys/SERVICE_API_KEY", json={"value": _VALUE}, headers=OWN
        )
        gone = await client.delete("/api/agents/ag_w/keys/SERVICE_API_KEY", headers=OWN)
    assert (listed.status_code, put.status_code, gone.status_code) == (403, 403, 403), (
        "keys are for the owner and admins"
    )


async def test_a_bad_name_or_a_member_only_name_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await agent_people(db_session)
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        reserved = await client.put(
            "/api/agents/ag_w/keys/LD_PRELOAD", json={"value": "x"}, headers=OWN
        )
        plain = await client.put(
            "/api/agents/ag_w/keys/REGION_NAME", json={"value": "x"}, headers=OWN
        )
        empty = await client.put(
            "/api/agents/ag_w/keys/SERVICE_API_KEY", json={"value": "  "}, headers=OWN
        )
    assert reserved.status_code == 422 and "reserved" in reserved.json()["detail"], reserved.text
    assert plain.status_code == 422, "a member may only add secret-looking names"
    assert empty.status_code == 422, "an empty value is refused"


async def test_an_admin_sets_a_name_a_member_cannot(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        put = await client.put(
            "/api/agents/ag_w/keys/REGION_NAME", json={"value": "eu-west-1"}, headers=OWN
        )
        listed = await client.get("/api/agents/ag_w/keys")
    assert put.status_code == 200, put.text
    assert [k["key"] for k in listed.json()] == ["REGION_NAME"], "an admin sees the keys"


async def test_an_owner_makes_lists_and_revokes_a_coding_key_and_sees_the_token_once(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await agent_people(db_session)
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(
        agents, owner.tenant_id, db_session_factory, token, settings=_settings()
    ) as client:
        minted = await client.post("/api/agents/ag_w/coding-keys", headers=OWN)
        listed = await client.get("/api/agents/ag_w/coding-keys")
        key_id = minted.json()["id"]
        revoked = await client.delete(f"/api/agents/ag_w/coding-keys/{key_id}", headers=OWN)
        after = await client.get("/api/agents/ag_w/coding-keys")
        again = await client.delete(f"/api/agents/ag_w/coding-keys/{key_id}", headers=OWN)
    body = minted.json()
    assert minted.status_code == 200, minted.text
    assert body["token"] in body["command"] and body["token"] in body["config"], (
        "the token is in the setup text it was made for"
    )
    assert body["token"] not in listed.text, "a listing never carries the token"
    assert [k["id"] for k in listed.json()] == [key_id], "the key is listed"
    assert listed.json()[0]["creator"] == "Test Person" and listed.json()[0]["mine"] is True, (
        "the maker is named"
    )
    assert revoked.status_code == 204 and after.json() == [], "revoking removes it"
    assert again.status_code == 404, "a revoked key is gone"
    async with db_session_factory() as session:
        actions = [
            r.action for r in await web_audit.list_recent(session, tenant_id=owner.tenant_id)
        ]
    assert "Created coding key" in actions and "Revoked coding key" in actions, (
        "both changes are audited"
    )


async def test_a_coding_key_is_made_only_for_who_it_would_work_for(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    other_token = await sign_in_token(db_session_factory, other)
    async with agent_client(
        agents, owner.tenant_id, db_session_factory, other_token, settings=_settings()
    ) as client:
        refused = await client.post("/api/agents/ag_w/coding-keys", headers=OWN)
    assert refused.status_code == 403, (
        "a workspace admin whose account is not a server admin and who does not own the "
        "agent would be turned away by the verifier, so no key is made"
    )
    async with db_session_factory.begin() as db:
        await db.execute(
            text("UPDATE accounts SET role = 'admin' WHERE id = :id"), {"id": other.account_id}
        )
    async with agent_client(
        agents, owner.tenant_id, db_session_factory, other_token, settings=_settings()
    ) as client:
        allowed = await client.post("/api/agents/ag_w/coding-keys", headers=OWN)
    assert allowed.status_code == 200, "a stored admin passes the verifier's rule"


async def test_coding_keys_need_the_install_to_be_set_up(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await agent_people(db_session)
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        minted = await client.post("/api/agents/ag_w/coding-keys", headers=OWN)
    assert minted.status_code == 503, "without a public address and signing secret there is no key"


async def test_a_member_sees_no_coding_keys_and_revokes_none(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    owner_token = await sign_in_token(db_session_factory, owner)
    async with agent_client(
        agents, owner.tenant_id, db_session_factory, owner_token, settings=_settings()
    ) as client:
        key_id = (await client.post("/api/agents/ag_w/coding-keys", headers=OWN)).json()["id"]
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        listed = await client.get("/api/agents/ag_w/coding-keys")
        revoked = await client.delete(f"/api/agents/ag_w/coding-keys/{key_id}", headers=OWN)
        minted = await client.post("/api/agents/ag_w/coding-keys", headers=OWN)
    assert listed.json() == [], "someone who is not an owner sees none"
    assert revoked.status_code == 404, "and cannot revoke the owner's key"
    assert minted.status_code in (403, 503), "and cannot make one"
    assert uuid.UUID(key_id), "the key id is a uuid"
